#!/usr/bin/env python3
"""Fast memory management — talks to ChromaDB directly, skipping Mem0/ONNX startup."""
import os
import sys
import json

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import chromadb

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory_store")
COLLECTION = "omniorb_memory"

def get_collection():
    client = chromadb.PersistentClient(path=DB_PATH)
    return client.get_collection(COLLECTION)

def list_memories():
    col = get_collection()
    data = col.get(include=["documents", "metadatas"])
    
    ids = data["ids"]
    docs = data["documents"]
    metas = data["metadatas"]
    
    if not ids:
        print("No memories found.")
        return

    print(f"{'='*80}")
    print(f" OmniOrb Long-Term Memories ({len(ids)} total)")
    print(f"{'='*80}\n")

    for i, (mid, doc, meta) in enumerate(zip(ids, docs, metas), 1):
        # Mem0 stores the actual memory text in metadata
        memory_text = meta.get("data", doc) if meta else doc
        # Try to parse JSON from data field
        if memory_text and memory_text.startswith("{"):
            try:
                parsed = json.loads(memory_text)
                memory_text = parsed.get("memory", parsed.get("data", memory_text))
            except json.JSONDecodeError:
                pass
        
        created = meta.get("created_at", "") if meta else ""
        updated = meta.get("updated_at", "") if meta else ""
        user = meta.get("user_id", "") if meta else ""
        
        print(f"  [{i}] ID: {mid}")
        print(f"      Memory: {memory_text}")
        if created:
            print(f"      Created: {created}")
        if updated and updated != created:
            print(f"      Updated: {updated}")
        print()

    print(f"{'='*80}")
    print(f"Usage: python {os.path.basename(__file__)} delete <ID>")

def delete_memory(memory_id):
    col = get_collection()
    col.delete(ids=[memory_id])
    print(f"Deleted: {memory_id}")

def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(f"Usage: python {os.path.basename(__file__)} [list|delete <ID>]")
        print(f"  list          Show all stored memories")
        print(f"  delete <ID>   Delete a memory by ID")
        return

    cmd = sys.argv[1].lower()
    if cmd == "list":
        list_memories()
    elif cmd == "delete":
        if len(sys.argv) < 3:
            print("Usage: python memories.py delete <ID>")
            sys.exit(1)
        delete_memory(sys.argv[2])
    else:
        # Default: treat no-arg as list
        list_memories()

if __name__ == "__main__":
    if len(sys.argv) == 1:
        list_memories()
    else:
        main()
