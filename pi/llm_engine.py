import logging
import threading
import time
import os
import re
import numpy as np
import subprocess

try:
    from google import genai
    from google.genai import types
    from google.api_core import exceptions
    LLM_AVAILABLE = True
except ImportError:
    LLM_AVAILABLE = False

try:
    from mem0 import MemoryClient
    MEM0_AVAILABLE = True
except ImportError:
    MEM0_AVAILABLE = False

import config

logger = logging.getLogger(__name__)

class LLMEngine:
    def __init__(self, callbacks):
            self.callbacks = callbacks
            if LLM_AVAILABLE:
                try:
                    self.client = genai.Client()
                except Exception as e:
                    logger.error("GenAI client init failed: %s", e)

    def process_audio(self, audio_array, is_continuity=False):
        # globals handled via callbacks
        '\n    LLM pipeline with streaming and Gemini function calling for timers:\n      1. Normalize + encode audio\n      2. Streaming first call — text fed sentence-by-sentence to Piper (TTS starts\n         on first sentence while LLM is still generating), function calls collected\n         as a side effect\n      3. If timer tool called: streaming follow-up call for spoken confirmation\n      4. self.callbacks.get("set_timer", lambda *a, **k: None)() sent to ESP32 only after confirmation TTS finishes\n    '
        if not LLM_AVAILABLE:
            logger.error('LLM libraries not installed!')
            self.callbacks.get("send_uart_command", lambda x: None)('TXT|AI missing')
            self.callbacks.get("send_uart_command", lambda x: None)('APP: ASSISTANT')
            return
        try:
            peak_amplitude = float(np.max(np.abs(audio_array)))
            if peak_amplitude < config.LLM_MIN_PEAK:
                logger.info(f'Recording discarded — silence (peak={peak_amplitude:.5f} < {config.LLM_MIN_PEAK})')
                with self.callbacks.get("get_state_lock", threading.Lock)():
                    self.callbacks.get("get_state_dict", lambda: {})()['status'] = 'IDLE'
                self.callbacks.get("send_uart_command", lambda x: None)('APP:ASSISTANT')
                return
            with self.callbacks.get("get_state_lock", threading.Lock)():
                self.callbacks.get("get_state_dict", lambda: {})()['processing'] = True
                self.callbacks.get("get_state_dict", lambda: {})()['status'] = 'THINKING'
            self.callbacks.get("send_uart_command", lambda x: None)('APP:THINKING')
            self.callbacks.get("speak_filler", lambda **kw: None)(is_continuity=is_continuity)
            logger.info(f'LLM query: {len(audio_array)} samples, peak={peak_amplitude:.5f}')
            peak = np.max(np.abs(audio_array))
            if peak > 0.001:
                audio_array = audio_array / peak * 0.95
            audio_ds = audio_array[::3]
            wav_rate = config.AUDIO_RATE // 3
            audio_int16 = (audio_ds * 32767).astype(np.int16)
            wav_buf = io.BytesIO()
            with wave.open(wav_buf, 'wb') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(wav_rate)
                wf.writeframes(audio_int16.tobytes())
            wav_bytes = wav_buf.getvalue()
            with self.callbacks.get("get_transcript_lock", threading.Lock)():
                self.callbacks.get("set_current_transcript", lambda x: None)('')
            interaction_transcript = ''
            user_msg = f'[Current Context: {self.callbacks.get("get_context", lambda *a, **k: "No context data")()}]'
            if is_continuity and _last_assistant_response:
                user_msg += f'\n[Your previous response was: "{_last_assistant_response}". The user is now following up.]'
            logger.info('LLM SYSTEM PROMPT: %s', config.LLM_SYSTEM_PROMPT)
            logger.info('LLM USER MESSAGE: %s', user_msg)
            audio_part = types.Part.from_bytes(data=wav_bytes, mime_type='audio/wav')
            cache_id = memory_manager.cache_id if memory_manager else None
            full_instr = memory_manager.full_system_instruction if memory_manager else config.LLM_SYSTEM_PROMPT
            _tool_cfg = types.ToolConfig(include_server_side_tool_invocations=True)
            if cache_id:
                logger.info('Using Context Cache: %s', cache_id)
                gen_cfg = types.GenerateContentConfig(cached_content=cache_id, tools=_ALL_TOOLS, tool_config=_tool_cfg)
            else:
                logger.info('Using Full System Instruction (No Cache)')
                gen_cfg = types.GenerateContentConfig(system_instruction=full_instr, tools=_ALL_TOOLS, tool_config=_tool_cfg)
            pending_timers = []
            fn_responses = []
            model_parts = []
            has_server_call = [False]
            stream1 = self.client.models.generate_content_stream(model=config.LLM_MODEL, contents=[audio_part, user_msg], config=gen_cfg)

            def _first_iter():
                for chunk in stream1:
                    if not chunk.candidates:
                        continue
                    candidate = chunk.candidates[0]
                    if not candidate.content or not candidate.content.parts:
                        continue
                    for part in candidate.content.parts:
                        model_parts.append(part)
                        if hasattr(part, 'function_call') and part.function_call:
                            fc = part.function_call
                            if fc.name == 'set_sleep_mode':
                                enabled = fc.args['enabled']
                                logger.info('Executing Tool: set_sleep_mode - %s', enabled)
                                self.callbacks.get("set_sleep_mode", lambda *a, **k: None)(enabled)
                                fn_responses.append(types.Part(function_response=types.FunctionResponse(name='set_sleep_mode', response={'status': 'success', 'is_sleeping': enabled}, id=fc.id)))
                            elif fc.name == 'set_timer':
                                secs = int(fc.args['seconds'])
                                label = str(fc.args.get('label', ''))
                                pending_timers.append((secs, label))
                                fn_responses.append(types.Part(function_response=types.FunctionResponse(name='set_timer', response={'status': 'success', 'message': f'Timer for {secs}s started.'}, id=fc.id)))
                            elif fc.name == 'send_detailed_email':
                                subject = fc.args['subject']
                                body = fc.args['body']
                                logger.info('Executing Tool: send_detailed_email - %s', subject)
                                result_holder = [False, 'Timed out']

                                def _run_email():
                                    result_holder[0], result_holder[1] = _send_email_task(subject, body)
                                t = threading.Thread(target=_run_email, daemon=True)
                                t.start()
                                speak_text(random.choice(['Routing that to your inbox.', 'Dispatching to your email now.', 'Transmitting to your inbox.', 'Sending that to your email.']))
                                t.join(timeout=15)
                                email_ok = result_holder[0]
                                email_msg = result_holder[1]
                                fn_responses.append(types.Part(function_response=types.FunctionResponse(name='send_detailed_email', response={'status': 'success' if email_ok else 'error', 'message': email_msg}, id=fc.id)))
                            elif fc.name == 'get_weather':
                                logger.info('Executing Tool: get_weather')
                                w_data = self.callbacks.get("get_weather", lambda *a, **k: "No weather data")()
                                logger.info('Tool Result: %s', w_data)
                                fn_responses.append(types.Part(function_response=types.FunctionResponse(name='get_weather', response={'status': 'success', 'data': w_data}, id=fc.id)))
                            elif fc.name == 'describe_camera_view':
                                logger.info('Executing Tool: describe_camera_view')
                                import camera_manager
                                speak_text(random.choice(['Let me take a look.', 'Capturing image.', 'Checking my camera.', 'Analyzing what is in front of me.']))
                                img_ok = camera_manager.capture_image('/tmp/last_capture.jpg')
                                if img_ok:
                                    try:
                                        with open('/tmp/last_capture.jpg', 'rb') as f:
                                            img_bytes = f.read()
                                        fn_responses.append(types.Part(function_response=types.FunctionResponse(name='describe_camera_view', response={'status': 'success', 'message': "Image captured successfully. Analyze this image to answer the user's request sardonically. You MUST start your response directly with the [TRANSCRIPT] tag, and output ONLY your final answer. Do NOT output any thought process, constraints, planning, draft, or instructions."}, id=fc.id)))
                                        fn_responses.append(types.Part.from_bytes(data=img_bytes, mime_type='image/jpeg'))
                                        logger.info('Camera image attached to tool response.')
                                    except Exception as e:
                                        logger.error('Error reading captured image: %s', e)
                                        fn_responses.append(types.Part(function_response=types.FunctionResponse(name='describe_camera_view', response={'status': 'error', 'message': f'Error reading captured image: {e}'}, id=fc.id)))
                                else:
                                    fn_responses.append(types.Part(function_response=types.FunctionResponse(name='describe_camera_view', response={'status': 'error', 'message': 'Failed to capture image from camera hardware. Ensure it is connected and functional.'}, id=fc.id)))
                            else:
                                logger.info('Server-side tool: %s', fc.name)
                                has_server_call[0] = True
                        if getattr(part, 'thought', False):
                            continue
                        txt = getattr(part, 'text', None)
                        if txt:
                            txt_lower = txt.lower().strip()
                            if txt_lower.startswith('thought') or txt_lower.startswith('- hook:') or txt_lower.startswith('- start with') or txt_lower.startswith('constraints:') or txt_lower.startswith('draft:'):
                                continue
                            yield part.text
            with self.callbacks.get("get_state_lock", threading.Lock)():
                self.callbacks.get("get_state_dict", lambda: {})()['status'] = 'SPEAKING'
            self.callbacks.get("send_uart_command", lambda x: None)('APP:SPEAKING')
            _, first_text = _speak_text_iter(_first_iter())
            with self.callbacks.get("get_transcript_lock", threading.Lock)():
                interaction_transcript = self.callbacks.get("get_current_transcript", lambda: "")()
            if self.callbacks.get("get_tts_abort_event", threading.Event)().is_set():
                return
            if fn_responses:
                logger.info('Triggering tool follow-up (Pass 2) with %s response(s).', len(fn_responses))
                stream2 = self.client.models.generate_content_stream(model=config.LLM_MODEL, contents=[audio_part, user_msg, types.Content(role='model', parts=model_parts), types.Content(role='user', parts=fn_responses)], config=types.GenerateContentConfig(cached_content=cache_id) if cache_id else types.GenerateContentConfig(system_instruction=full_instr))

                def _follow_iter():
                    for chunk in stream2:
                        if not chunk.candidates:
                            continue
                        candidate = chunk.candidates[0]
                        if not candidate.content or not candidate.content.parts:
                            continue
                        for part in candidate.content.parts:
                            if getattr(part, 'thought', False):
                                continue
                            txt = getattr(part, 'text', None)
                            if txt:
                                txt_lower = txt.lower().strip()
                                if txt_lower.startswith('thought') or txt_lower.startswith('- hook:') or txt_lower.startswith('- start with') or txt_lower.startswith('constraints:') or txt_lower.startswith('draft:'):
                                    continue
                                yield part.text
                _, full_text = _speak_text_iter(_follow_iter())
            elif has_server_call[0]:
                logger.info('google_search fired — follow-up stream for grounded response.')
                stream2 = self.client.models.generate_content_stream(model=config.LLM_MODEL, contents=[audio_part, user_msg, types.Content(role='model', parts=model_parts)], config=gen_cfg)

                def _search_follow_iter():
                    for chunk in stream2:
                        if not chunk.candidates:
                            continue
                        candidate = chunk.candidates[0]
                        if not candidate.content or not candidate.content.parts:
                            continue
                        for part in candidate.content.parts:
                            if getattr(part, 'thought', False):
                                continue
                            txt = getattr(part, 'text', None)
                            if txt:
                                txt_lower = txt.lower().strip()
                                if txt_lower.startswith('thought') or txt_lower.startswith('- hook:') or txt_lower.startswith('- start with') or txt_lower.startswith('constraints:') or txt_lower.startswith('draft:'):
                                    continue
                                yield part.text
                _, full_text = _speak_text_iter(_search_follow_iter())
                if not full_text:
                    logger.warning('google_search follow-up returned no text.')
                    full_text = first_text
            else:
                full_text = first_text
            for secs, label in pending_timers:
                self.callbacks.get("set_timer", lambda *a, **k: None)(secs, label)
            display_text = ''
            if full_text:
                display_text = _TRANS_PATTERN.sub('', full_text).strip()
                _last_assistant_response = display_text
                logger.info('LLM answer: %s', display_text)
                self.callbacks.get("send_uart_command", lambda x: None)(f"TXT|{display_text.replace(chr(10), ' ')}")
                with self.callbacks.get("get_transcript_lock", threading.Lock)():
                    final_trans = self.callbacks.get("get_current_transcript", lambda: "")()
                if memory_manager:
                    threading.Thread(target=memory_manager.add_interaction, args=(final_trans or '(no transcript)', display_text), daemon=True).start()
                if _memory and final_trans:

                    def _store_mem_bg(txt):
                        try:
                            _memory.add(txt, user_id='primary_user')
                            logger.info('Turn committed to long-term memory (background).')
                        except Exception as e:
                            logger.warning('Memory storage failed: %s', e)
                    threading.Thread(target=_store_mem_bg, args=(final_trans,), daemon=True).start()
            with self.callbacks.get("get_state_lock", threading.Lock)():
                is_slp = assistant_state.get('is_sleeping', False)
            if is_slp:
                with self.callbacks.get("get_state_lock", threading.Lock)():
                    self.callbacks.get("get_state_dict", lambda: {})()['status'] = 'IDLE'
                global _volume
                with _volume_lock:
                    _volume = 0
                logger.info('Sleep Mode active: Muting volume and bypassing continuity.')
            elif not is_exit_command(interaction_transcript) and (not is_exit_command(display_text)):
                with self.callbacks.get("get_state_lock", threading.Lock)():
                    self.callbacks.get("get_state_dict", lambda: {})()['status'] = 'CONTINUITY'
                    self.callbacks.get("get_state_dict", lambda: {})()['continuity_until'] = time.time() + config.CONTINUITY_TIMEOUT
                self.callbacks.get("send_uart_command", lambda x: None)('APP:CONTINUITY')
                logger.info('Transitioned to CONTINUITY state (%ss window).', config.CONTINUITY_TIMEOUT)
            else:
                with self.callbacks.get("get_state_lock", threading.Lock)():
                    self.callbacks.get("get_state_dict", lambda: {})()['status'] = 'IDLE'
                self.callbacks.get("send_uart_command", lambda x: None)('APP:ASSISTANT')
                logger.info('Exit command detected or no follow-up needed. Returning to IDLE.')
        except Exception as e:
            logger.error('LLM pipeline error: %s', e)
            self.callbacks.get("send_uart_command", lambda x: None)('TXT|Sorry, I had an error.')
            speak_text('Sorry, I had an error.')
            with self.callbacks.get("get_state_lock", threading.Lock)():
                self.callbacks.get("get_state_dict", lambda: {})()['status'] = 'IDLE'
            self.callbacks.get("send_uart_command", lambda x: None)('APP: ASSISTANT')
        finally:
            _tts_active.clear()
            with self.callbacks.get("get_state_lock", threading.Lock)():
                self.callbacks.get("get_state_dict", lambda: {})()['processing'] = False
                self.callbacks.get("get_state_dict", lambda: {})()['wakeword_cooldown_until'] = time.time() + config.WAKEWORD_POST_LLM_COOLDOWN