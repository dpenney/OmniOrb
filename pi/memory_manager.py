import json
import logging
import os
import threading
import time
from datetime import datetime
import config

logger = logging.getLogger(__name__)

class MemoryManager:
    def __init__(self, client):
        self.client = client
        self.session_history = []
        self.last_interaction_time = time.monotonic()
        self.cache_id = None
        self.lock = threading.Lock()
        
        # Load last 5 summaries from log
        self.summaries = self._load_summaries()
        
        # Start background timer for session rolling
        self.stop_event = threading.Event()
        self.timer_thread = threading.Thread(target=self._monitor_inactivity, daemon=True)
        self.timer_thread.start()
        
        # Initialize/Refresh context cache on startup
        self.refresh_cache()

    def _load_summaries(self):
        if not os.path.exists(config.SUMMARY_LOG):
            return []
        try:
            with open(config.SUMMARY_LOG, 'r') as f:
                lines = f.readlines()
                # Return content without the timestamp bracket
                return [line.split('] ', 1)[1].strip() if '] ' in line else line.strip() for line in lines[-5:]]
        except Exception as e:
            logger.error(f"Error loading summaries: {e}")
            return []

    def _save_summary(self, summary):
        try:
            with open(config.SUMMARY_LOG, 'a', encoding='utf-8') as f:
                f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] {summary}\n")
            # Update internal list
            self.summaries = self._load_summaries()
        except Exception as e:
            logger.error(f"Error saving summary: {e}")

    def get_full_system_instruction(self):
        """Consolidates base prompt, static memories, and recent summaries."""
        static_memories = ""
        if os.path.exists(config.MEMORY_FILE):
            try:
                with open(config.MEMORY_FILE, 'r', encoding='utf-8') as f:
                    mems = json.load(f)
                    static_memories = "\n".join([m.get('content', '') for m in mems])
            except Exception as e:
                logger.error(f"Error loading memory file: {e}")

        summaries_block = "\n".join([f"- {s}" for s in self.summaries])
        return (
            config.LLM_SYSTEM_PROMPT + 
            "\n\n### STATIC MEMORY CACHE\n" + static_memories +
            "\n\n### RECENT SESSION SUMMARIES\n" + (summaries_block if summaries_block else "No prior summaries.")
        )

    def refresh_cache(self):
        """Creates or updates the Gemini Context Cache (Tier 1) only if above threshold."""
        logger.info("Refreshing Memory State...")
        
        self.full_system_instruction = self.get_full_system_instruction()

        try:
            # 1. Threshold Check (Tier 1 Optimization)
            # Standard threshold for Gemini 1.5/2.0 Flash is 32,768 tokens.
            res = self.client.models.count_tokens(
                model=config.LLM_MODEL,
                contents=self.full_system_instruction
            )
            token_count = res.total_tokens
            logger.info(f"Memory Payload: {token_count} tokens")

            if token_count >= 32768:
                # 2. Above threshold: Use Explicit Context Caching (Tier 1)
                cache = self.client.caches.create(
                    model=config.LLM_MODEL,
                    config={
                        'display_name': 'omniorb_tiered_memory',
                        'system_instruction': self.full_system_instruction,
                        'ttl': f"{config.CACHE_TTL_SECONDS}s"
                    }
                )
                self.cache_id = cache.name
                logger.info(f"Context Cache created: {self.cache_id}")
            else:
                # 3. Below threshold: Use standard system_instruction (Zero cost/delay)
                self.cache_id = None
                logger.info("Below caching threshold. Standard system_instruction will be used.")

        except Exception as e:
            # Barksdale "Safety" Fallback: Default to full context if Cache API fails
            logger.error(f"Memory lifecycle error (Safety fallback to Full Context): {e}")
            self.cache_id = None

    def heartbeat(self):
        """Extends Cache TTL on interaction (Tier 2). Only if cache exists."""
        self.last_interaction_time = time.monotonic()
        if self.cache_id:
            try:
                self.client.caches.update(
                    name=self.cache_id,
                    config={'ttl': f"{config.CACHE_TTL_SECONDS}s"}
                )
                logger.info("Context Cache TTL extended (Heartbeat).")
            except Exception as e:
                logger.warning(f"Cache heartbeat failed, trying to refresh: {e}")
                self.refresh_cache()

    def add_interaction(self, user_text, model_text):
        """Records a turn in the session history and pulses heartbeat."""
        with self.lock:
            self.session_history.append({"user": user_text, "model": model_text})
        self.heartbeat()

    def _monitor_inactivity(self):
        while not self.stop_event.is_set():
            time.sleep(60) # Check every minute
            if time.monotonic() - self.last_interaction_time > config.SESSION_TIMEOUT_SECONDS:
                if self.session_history:
                    logger.info("Session timeout triggered (45m inactivity). Running Summary Task...")
                    self.run_summary_task()

    def run_summary_task(self):
        """Summarizes the current session and appends to log (Tier 3)."""
        if not self.session_history:
            return

        with self.lock:
            interaction_data = self.session_history[:]
            self.session_history = [] # Clear history for next session

        history_text = "\n".join([f"User: {t['user']}\nAI: {t['model']}" for t in interaction_data])
        prompt = (
            "Summarize the following interaction between a user and an AI in exactly 3 concise sentences. "
            "Focus strictly on key facts learned or major topics discussed. "
            "Do not use introductory phrases like 'In this interaction...'.\n\n"
            f"--- INTERACTION START ---\n{history_text}\n--- INTERACTION END ---"
        )

        try:
            # Generate summary (Tier 3)
            response = self.client.models.generate_content(
                model=config.LLM_MODEL,
                contents=prompt
            )
            summary = response.text.strip()
            self._save_summary(summary)
            logger.info(f"Session summary saved: {summary}")
            
            # Re-initialize cache with the new summary included for the NEXT session
            self.refresh_cache()
        except Exception as e:
            logger.error(f"Error generating session summary: {e}")

    def stop(self):
        self.stop_event.set()
        if self.timer_thread.is_alive():
            self.timer_thread.join()
