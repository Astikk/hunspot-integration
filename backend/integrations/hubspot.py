# slack.py
import asyncio, base64, httpx, json, secrets
from dotenv import load_dotenv
from datetime import datetime
from fastapi.responses import HTMLResponse
from fastapi import Request, HTTPException
from integrations.integration_item import IntegrationItem
from redis_client import add_key_value_redis, get_value_redis, delete_key_redis
import os

load_dotenv()

CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
encoded_client_id_secret = base64.b64encode(f'{CLIENT_ID}:{CLIENT_SECRET}'.encode()).decode()

REDIRECT_URI = "http://localhost:8000/integrations/hubspot/oauth2callback"
authorization_url = f"https://app.hubspot.com/oauth/authorize?client_id={CLIENT_ID}&scope=oauth%20crm.objects.contacts.read&redirect_uri={REDIRECT_URI}"

async def authorize_hubspot(user_id, org_id):
    state_data = {
        'state': secrets.token_urlsafe(32),
        'user_id': user_id,
        'org_id': org_id
    }
    encoded_state = json.dumps(state_data)
    await add_key_value_redis(f'hubspot_state:{org_id}:{user_id}', encoded_state, expire=600)

    return f'{authorization_url}&state={encoded_state}'

async def oauth2callback_hubspot(request: Request):
    if request.query_params.get('error'):
        raise HTTPException(status_code=400, detail=request.query_params.get('error'))
    code = request.query_params.get('code')
    encoded_state = request.query_params.get('state')
    state_data = json.loads(encoded_state)

    original_state = state_data.get('state')
    user_id = state_data.get('user_id')
    org_id = state_data.get('org_id')

    saved_state = await get_value_redis(f'hubspot_state:{org_id}:{user_id}')

    if not saved_state or original_state != json.loads(saved_state).get('state'):
        raise HTTPException(status_code=400, detail='State does not match.')
    async with httpx.AsyncClient() as client:
        response,_ = await asyncio.gather(
            client.post("https://api.hubapi.com/oauth/v1/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": REDIRECT_URI,
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ),
            delete_key_redis(f'hubspot_state:{org_id}:{user_id}'),
        )
        print(response)
    if response.status_code != 200:
        raise HTTPException(status_code=400, detail='Something went wrong...')

    await add_key_value_redis(f'hubspot_credentials:{org_id}:{user_id}', json.dumps(response.json()), expire=600)

    html_content = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Authentication Complete</title>
        </head>
        <body>
            <script>
            if (window.opener) {
                window.opener.postMessage({ success: true }, "*");
            }
            window.close();
            </script>
            <p>You can close this window now.</p>
        </body>
        </html>
    """
    return HTMLResponse(content=html_content)

async def get_hubspot_credentials(user_id, org_id):
    credentials = await get_value_redis(f'hubspot_credentials:{org_id}:{user_id}')
    if not credentials:
        raise HTTPException(status_code=400, detail='No credentials found.')
    credentials = json.loads(credentials)
    if not credentials:
        raise HTTPException(status_code=400, detail='No credentials found.')
    await delete_key_redis(f'hubspot_credentials:{org_id}:{user_id}')

    return credentials

async def create_integration_item_metadata_object(response_json):
    properties = response_json.get("properties", {})
    full_name = f"{properties.get('firstname', '')} {properties.get('lastname', '')}".strip()

    return IntegrationItem(
        id=response_json.get("id"),
        type="hubspot_contact",
        name=full_name,
        creation_time=datetime.fromisoformat(properties.get("createdate").replace("Z", "+00:00")),
        last_modified_time=datetime.fromisoformat(properties.get("lastmodifieddate").replace("Z", "+00:00")),
        url=None,  # Optional: HubSpot contact URL if you can construct it
    )

async def get_items_hubspot(credentials):
    credentials = json.loads(credentials)
    access_token = credentials.get("access_token")
    if not access_token:
        raise HTTPException(status_code=400, detail="Missing access token")

    headers = {
        "Authorization": f"Bearer {access_token}",
    }

    url = "https://api.hubapi.com/crm/v3/objects/contacts"
    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers)
    print("response", response)
    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail="Failed to fetch contacts")

    contacts = response.json().get("results", [])
    integration_items = []
    for contact in contacts:
        item = await create_integration_item_metadata_object(contact)
        integration_items.append(item)

    return integration_items
