import os
import sys
import logging

# Ensure we are in the right directory to find config
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import config
from mem0 import Memory
from dotenv import load_dotenv

# Load secret environment variables from .env
load_dotenv()

# Map GEMINI_API_KEY to GOOGLE_API_KEY for libraries that expect it
if os.getenv("GEMINI_API_KEY") and not os.getenv("GOOGLE_API_KEY"):
    os.environ["GOOGLE_API_KEY"] = os.getenv("GEMINI_API_KEY")

# Force Google AI API version to v1
os.environ["GOOGLE_API_VERSION"] = "v1"

# Suppress noisy logs
logging.getLogger('mem0').setLevel(logging.ERROR)

def view_memories():
    # Configuration matches assistant_brains.py exactly
    mem_config = {
        "llm": {
            "provider": "gemini",
            "config": {
                "model": config.LLM_MODEL,
            }
        },
        "embedder": {
            "provider": "fastembed",
            "config": {
                "model": "BAAI/bge-small-en-v1.5",
            }
        },
        "vector_store": {
            "provider": "chroma",
            "config": {
                "collection_name": "omniorb_memory",
                "path": os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory_store"),
            }
        }
    }

    try:
        if "GOOGLE_API_KEY" not in os.environ:
             os.environ["GOOGLE_API_KEY"] = os.getenv("GOOGLE_API_KEY", "")

        memory = Memory.from_config(mem_config)
        raw_output = memory.get_all(user_id="primary_user")
        
        # Handle cases where get_all returns a dict with 'results' key or a direct list
        if isinstance(raw_output, dict):
            mems = raw_output.get("results", [])
            count = raw_output.get("count", len(mems))
        else:
            mems = raw_output
            count = len(mems)
            
        if not mems:
            print("No system memories found.")
        else:
            print(f"\n🧠 OmniOrb Memory Vault ({count} facts found)")
            print("=" * 50)
            for m in mems:
                # Handle both dict and object formats
                if isinstance(m, dict):
                    id = m.get("id", "N/A")
                    text = m.get("text") or m.get("memory") or str(m)
                    timestamp = m.get("created_at", "N/A")
                else:
                    id = getattr(m, "id", "N/A")
                    text = getattr(m, "text", getattr(m, "memory", str(m)))
                    timestamp = getattr(m, "created_at", "N/A")
                    
                # Clean up timestamp if possible
                if timestamp and "T" in str(timestamp):
                    timestamp = str(timestamp).split(".")[0].replace("T", " ")
                    
                print(f"ID: {id} | [{timestamp}] {text}")
            print("=" * 50)
    except Exception as e:
        print(f"Error accessing memory: {e}")

if __name__ == "__main__":
    view_memories()
