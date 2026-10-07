import decimal
import requests
import json
import datetime
import boto3
import os
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

dynamodb = boto3.resource('dynamodb')
calendar_table_name = os.environ.get('DYNAMODB_CALENDAR', 'dummy-name-for-testing')
calendar_table = dynamodb.Table(calendar_table_name)


def create_response(status_code: int, message):
    """Returns a given status code."""

    return {
        'statusCode': status_code,
        'headers': {
            # Required for CORS support to work
            'Access-Control-Allow-Origin': '*',
            # Required for cookies, authorization headers with HTTPS
            'Access-Control-Allow-Credentials': 'true',
        },
        'body': message
    }


class DecimalEncoder(json.JSONEncoder):
    """Helper class to convert a DynamoDB item to JSON."""

    def default(self, o):
        if isinstance(o, set):
            return list(o)
        if isinstance(o, decimal.Decimal):
            if o % 1 > 0:
                return float(o)
            else:
                return int(o)
        return super(DecimalEncoder, self).default(o)


def get_utc_iso_time():
    """Returns formatted UTC datetime string of current time."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def strip_trailing_z(input_string):
    if input_string and input_string[-1] == "Z":
        return input_string[:-1]
    return input_string


def create_calendar_event(event):
    return calendar_table.put_item(Item=event)


def get_event_by_id(eventId, eventStart):
    """Returns details of a requested event from the calendar database."""

    print(f'eventId: {eventId}')
    print(f'eventStart: {eventStart}')
    try:
        response = calendar_table.get_item(
            Key={
                'event_id': eventId,
                'start': eventStart,
            }
        )
        print(f"get_event_by_id response: {response}")
        return response['Item']
    except Exception as e:
        print(f"error with get_event_by_id")
        print(e)
    return ''


def get_events_during_time(time, site):
    """Gets calendar events at a site that are active during a given time.

    Args:
        time (str): UTC datestring (eg. '2022-05-14T17:30:00Z').
        site (str): sitecode (eg. 'saf').

    Returns:
        A list of event objects matching time and site criteria.
    """

    response = calendar_table.query(
        IndexName="site-end-index",
        KeyConditionExpression=
                Key('site').eq(site)
                & Key('end').gte(time),
        FilterExpression=Key('start').lte(time)
    )
    print(f"Items during {time}: {response['Items']}")
    return response['Items']


def get_projects_url(path):
    # PTR_PROJECTS_ROOT first, which is how every other component in this stack
    # is told where the services are. Without it this function could only ever
    # name projects.photonranch.org, so a deployment with its own projects
    # backend -- the Proxmox lab, or anything run offline -- would reach across
    # to LCO production instead of to itself. Reads and writes both go through
    # here, so that was not merely a wrong answer: deleting a project locally
    # would have aimed the cleanup at production's calendar.
    root = os.getenv('PTR_PROJECTS_ROOT')
    if root:
        return f"{root.rstrip('/')}/{path}"

    # Otherwise the original behaviour: use the same projects deployment as the
    # one running the calendar. E.g. The dev calendar backend will call the dev
    # projects backend
    stage = os.getenv('STAGE', 'dev')
    # The production projects url replaces 'prod' with 'projects' in the url
    if stage == 'prod':
        stage = 'projects'

    url = f"https://projects.photonranch.org/{stage}/{path}"
    return url


def associate_event_with_project(project_id, event_id):
    """Record a booking on the project it will run, at booking time.

    A project keeps scheduled_with_events so that deleting it can also clear
    the bookings that would have run it. Nothing ever wrote that list: the
    booking carried project_id and the project was never told, so every project
    in the table had an empty list and deleteProject's cleanup removed nothing.
    Deleting a project therefore left its bookings behind, and an observatory
    would pick one up, find no project, and silently skip it -- six such
    bookings had accumulated across five sites before anyone looked.

    project_id is "<project_name>#<created_at>"; anything else, including the
    stored 'none', means the booking has no project to register with.

    Never raises. A booking that is made but not registered is the condition
    this stack has been in all along, and it is a far better outcome than
    refusing the booking because the projects backend is briefly unwell. The
    add-project-event endpoint is idempotent, so a retry is harmless.
    """
    if not project_id or '#' not in str(project_id):
        return
    project_name, created_at = str(project_id).split('#', 1)
    try:
        requests.post(
            get_projects_url('add-project-event'),
            json.dumps({
                "project_name": project_name,
                "created_at": created_at,
                "event_id": event_id,
            }),
            timeout=10,
        )
    except Exception as e:
        print(f"could not associate event {event_id} with project {project_id}: {e}")


def get_project(project_name, created_at):
    """Get project details from the projects backend.

    Args:
        project_name (str):
            Name of the project in the projects-{stage} database.
        created_at (str):
            UTC datestring at creation (eg. '2022-05-14T17:30:00Z').

    Returns:
        Requested project details JSON, if response code 200.
    """
    url = get_projects_url('get-project')
    body = json.dumps({
        "project_name": project_name,
        "created_at": created_at,
    })
    response = requests.post(url, body)
    if response.status_code == 200:
        return response.json()
    else:
        return "Project not found."


def delete_calendar_event(event_id, start_time, user_making_request=None, requester_is_admin=True):
    """Deletes an event from the DynamoDB table with optional authorization check."""
    try:
        # Perform the delete operation
        response = calendar_table.delete_item(
            Key={
                'event_id': event_id,
                'start': start_time
            },
            ConditionExpression=":requesterIsAdmin = :true OR creator_id = :requester_id",
            ExpressionAttributeValues = {
                ":requester_id": user_making_request,
                ":requesterIsAdmin": requester_is_admin,
                ":true": True
            }
        )
        return response  # Return the raw response data (typically including `Item` if success)
    except ClientError as e:
        # Return None to indicate failure
        # In the future, handle different error codes as needed here
        print(f"error deleting event: {e}")
        return None
