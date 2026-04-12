import os
import sys
import logging

# Setup basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("DIAG")

def run_diag():
    print("\n--- OMNIORB MEMORY DIAGNOSTIC ---\n")
    
    # 1. Check Python version
    print(f"[1] Python version: {sys.version}")

    # 2. Test Imports
    print("[2] Testing imports...")
    try:
        import mem0
        print(f"    SUCCESS: mem0 imported (version: {getattr(mem0, '__version__', 'unknown')})")
    except ImportError as e:
        print(f"    FAILED: mem0 import failed: {e}")
        return

    try:
        import chromadb
        print(f"    SUCCESS: chromadb imported (version: {getattr(chromadb, '__version__', 'unknown')})")
    except ImportError as e:
        print(f"    FAILED: chromadb import failed: {e}")
        return

    try:
        from google import genai
        print(f"    SUCCESS: google-genai imported")
    except ImportError as e:
        print(f"    FAILED: google-genai import failed: {e}")
        return

    # 3. Check environment
    print("[3] Checking environment...")
    from dotenv import load_dotenv
    load_dotenv()
    
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if api_key:
        print(f"    SUCCESS: API Key found (starts with: {api_key[:8]}...)")
        os.environ["GOOGLE_API_KEY"] = api_key
    else:
        print("    FAILED: No API key found in .env or environment")
        return

    # Force v1
    os.environ["GOOGLE_API_VERSION"] = "v1"

    # 4. Test Memory Class
    print("[4] Testing Memory class initialization...")
    from mem0 import Memory
    
    config = {
        "llm": {
            "provider": "gemini",
            "config": {
                "model": "gemini-2.5-flash-lite",
                "temperature": 0.1,
            }
        },
        "embedder": {
            "provider": "gemini",
            "config": {
                "model": "text-embedding-004", 
            }
        },
        "vector_store": {
            "provider": "chroma",
            "config": {
                "collection_name": "diag_test",
                "path": "./memory_store_diag",
            }
        }
    }

    try:
        memory = Memory.from_config(config)
        print("    SUCCESS: Memory initialized from config")
    except Exception as e:
        print(f"    FAILED: Memory initialization: {e}")
        import traceback
        traceback.print_exc()
        return

    # 5. Test Model Listing (Sanity check)
    print("[5] Testing API model access...")
    try:
        client = genai.Client(api_key=api_key)
        models = [m.name for m in client.models.list()]
        has_embed = any("embedding" in m for m in models)
        print(f"    SUCCESS: Found {len(models)} models. Embedding accessible: {has_embed}")
        if not has_embed:
             print(f"    WARNING: No embedding models in: {models}")
    except Exception as e:
        print(f"    FAILED: API model listing: {e}")

    # 6. Test Memory Write
    print("[6] Testing Memory.add()...")
    try:
        memory.add("This is a diagnostic test.", user_id="diag_user")
        print("    SUCCESS: Memory written successfully!")
    except Exception as e:
        print(f"    FAILED: Memory write: {e}")
        return

    # 7. Test Retrieval
    print("[7] Testing Memory.get_all()...")
    try:
        mems = memory.get_all(user_id="diag_user")
        print(f"    SUCCESS: Retrieved {len(mems)} items.")
    except Exception as e:
        print(f"    FAILED: Memory retrieval: {e}")

    print("\n--- DIAGNOSTIC COMPLETE ---\n")

if __name__ == "__main__":
    run_diag()
