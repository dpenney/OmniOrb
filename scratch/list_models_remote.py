from google import genai
import os

print("--- v1 Models ---")
client_v1 = genai.Client(api_key=os.getenv('GEMINI_API_KEY'), http_options={'api_version': 'v1'})
try:
    for m in client_v1.models.list():
        print(m.name)
except Exception as e:
    print(f"Error listing v1: {e}")

print("\n--- v1beta Models ---")
client_beta = genai.Client(api_key=os.getenv('GEMINI_API_KEY'), http_options={'api_version': 'v1beta'})
try:
    for m in client_beta.models.list():
        print(m.name)
except Exception as e:
    print(f"Error listing v1beta: {e}")
