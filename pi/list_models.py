import os
from google import genai

# Setup API Key
api_key = os.getenv("GOOGLE_API_KEY")
if not api_key:
    from dotenv import load_dotenv
    load_dotenv()
    api_key = os.getenv("GOOGLE_API_KEY")

client = genai.Client(api_key=api_key)

print("Listing ALL models available:")
try:
    for m in client.models.list():
        print(f"- {m.name} (actions: {m.supported_actions})")
except Exception as e:
    print(f"Error listing models: {e}")
