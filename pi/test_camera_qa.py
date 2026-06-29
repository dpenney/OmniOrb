import os
import sys
import logging
from dotenv import load_dotenv

# Set paths
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Load env before imports
load_dotenv()
if os.getenv("GEMINI_API_KEY") and not os.getenv("GOOGLE_API_KEY"):
    os.environ["GOOGLE_API_KEY"] = os.getenv("GEMINI_API_KEY")
os.environ["GOOGLE_API_VERSION"] = "v1"

import config
import camera_manager
from google import genai
from google.genai import types

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("test_camera_qa")

def main():
    if not os.getenv("GEMINI_API_KEY"):
        logger.error("GEMINI_API_KEY environment variable is not set!")
        sys.exit(1)
        
    client = genai.Client()
    
    # 1. Define tools (matches assistant_brains.py)
    _CAMERA_TOOL = types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name="describe_camera_view",
            description="Capture a real-time image from the camera to see what is currently in front of the device. Use this whenever the user asks a question about what is in front of the device, what it is looking at, or asks for description of objects, people, or surroundings in the room.",
            parameters=types.Schema(
                type="OBJECT",
                properties={},
                required=[],
            )
        )
    ])
    
    user_query = "What is in front of you? Describe it."
    logger.info(f"User Query: {user_query}")
    
    gen_cfg = types.GenerateContentConfig(
        system_instruction=config.LLM_SYSTEM_PROMPT,
        tools=[_CAMERA_TOOL],
        tool_config=types.ToolConfig(include_server_side_tool_invocations=True),
    )
    
    logger.info("Sending first turn to Gemini...")
    response = client.models.generate_content(
        model=config.LLM_MODEL,
        contents=user_query,
        config=gen_cfg
    )
    
    model_parts = []
    fn_responses = []
    
    if response.candidates and response.candidates[0].content.parts:
        for part in response.candidates[0].content.parts:
            model_parts.append(part)
            if hasattr(part, 'function_call') and part.function_call:
                fc = part.function_call
                logger.info(f"Received Function Call request: {fc.name}")
                
                if fc.name == "describe_camera_view":
                    logger.info("Executing describe_camera_view locally...")
                    # Capture frame (will handle missing hardware gracefully)
                    img_ok = camera_manager.capture_image("/tmp/last_capture.jpg")
                    if img_ok:
                        logger.info("Frame captured successfully!")
                        with open("/tmp/last_capture.jpg", "rb") as f:
                            img_bytes = f.read()
                        
                        fn_responses.append(types.Part(function_response=types.FunctionResponse(
                            name="describe_camera_view",
                            response={"status": "success", "message": "Image captured successfully."},
                            id=fc.id
                        )))
                        fn_responses.append(types.Part.from_bytes(data=img_bytes, mime_type="image/jpeg"))
                    else:
                        logger.warning("Camera capture failed (likely hardware disconnected). Sending error status to Gemini.")
                        fn_responses.append(types.Part(function_response=types.FunctionResponse(
                            name="describe_camera_view",
                            response={"status": "error", "message": "Failed to capture image. Camera hardware not responding or disconnected."},
                            id=fc.id
                        )))
            elif getattr(part, 'text', None):
                print(f"Model response: {part.text}")
                
    if fn_responses:
        logger.info("Sending tool response to Gemini for second turn...")
        response2 = client.models.generate_content(
            model=config.LLM_MODEL,
            contents=[
                user_query,
                types.Content(role="model", parts=model_parts),
                types.Content(role="user", parts=fn_responses)
            ],
            config=types.GenerateContentConfig(
                system_instruction=config.LLM_SYSTEM_PROMPT
            )
        )
        print("Final Answer from Gemini:")
        print(response2.text)
    else:
        logger.info("No tool calls were requested by the model.")

if __name__ == "__main__":
    main()
