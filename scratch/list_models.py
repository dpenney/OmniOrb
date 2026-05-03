from google import genai
client = genai.Client(http_options={'api_version': 'v1alpha'})
for m in client.models.list():
    print(m.name)
