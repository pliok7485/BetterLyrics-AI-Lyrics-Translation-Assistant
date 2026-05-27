import threading
import requests
import json
import os
import traceback
import datetime
import concurrent.futures
from flask import Flask, request, jsonify
import tkinter as tk
from tkinter import scrolledtext, ttk

CONFIG_FILE = "betterlyrics_profiles.json"
DEBUG_LOG_FILE = "betterlyrics_debug.log"
MAX_DEBUG_LOG_SIZE = 5 * 1024 * 1024  # 5MB

def log_debug(level, message, detail=""):
    """寫入 Debug 日誌到檔案，並自動截斷到 5MB"""
    try:
        # 日誌檔案太大時截斷
        if os.path.exists(DEBUG_LOG_FILE) and os.path.getsize(DEBUG_LOG_FILE) > MAX_DEBUG_LOG_SIZE:
            with open(DEBUG_LOG_FILE, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            # 保留最後 1000 行
            with open(DEBUG_LOG_FILE, 'w', encoding='utf-8') as f:
                f.writelines(lines[-1000:])
        
        timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        thread_name = threading.current_thread().name
        log_entry = f"[{timestamp}] [{thread_name}] [{level}] {message}"
        if detail:
            log_entry += f"\nDETAIL: {detail}"
        log_entry += "\n"
        
        with open(DEBUG_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(log_entry)
    except:
        pass  # 日誌系統不應該拋出例外


class TranslationServer:
    def __init__(self, log_widget):
        self.log_widget = log_widget
        self.app = Flask(__name__)
        self.mode = "Google"
        self.api_key = ""
        self.api_url = ""
        self.model_name = "gpt-3.5-turbo"
        self.system_prompt = "你是一位專業的歌詞翻譯家。請將以下歌詞逐行翻譯為台灣繁體中文（使用台灣慣用語，嚴禁簡體）。\n\n【翻譯原則】\n1. 保留原文情感、語氣和意境\n2. 用詞貼切自然，符合中文歌詞的表達習慣\n3. 專有名詞、人名、品牌名原則上保留原文不翻譯\n4. 押韻或節奏感可適度調整，讓翻譯唱起來也順口\n\n【格式規則】\n- 行數必須完全一致，空行保持空行\n- 每行一對一翻譯，不要合併或拆分任何行\n- 只輸出翻譯結果，不加任何說明文字"
        self.is_active = False
        self.temperature = 0.1
        self.cache = {}  # 翻譯快取：{ (mode, text): translated_text }
        self.translation_count = 0  # 翻譯計數器
        log_debug("INFO", "TranslationServer 初始化完成")
        self.setup_routes()

    def log(self, message):
        self.log_widget.insert(tk.END, f"{message}\n")
        self.log_widget.see(tk.END)

    def setup_routes(self):
        @self.app.route('/translate', methods=['POST', 'GET'])
        @self.app.route('/api/v1/translate', methods=['POST', 'GET'])
        def translate():
            if not self.is_active:
                log_debug("WARN", "伺服器未啟動，拒絕請求")
                return jsonify({"error": "Server stopped"}), 503
            data = request.get_json(force=True, silent=True) or {}
            q = data.get('q') or request.form.get('q') or request.args.get('q', '')
            if not q:
                log_debug("WARN", "收到空內容請求")
                return jsonify({"translatedText": ""})
            
            text_preview = q[:80].replace('\n', '\\n') + ("..." if len(q) > 80 else "")
            log_debug("INFO", f"收到翻譯請求: {len(q)} 字元, 預覽={text_preview}")
            
            try:
                result = self.do_translate(q)
                log_debug("INFO", "翻譯請求完成，回傳 response")
                return jsonify({"translatedText": result})
            except Exception as e:
                error_msg = f"do_translate 拋出未捕捉例外: {str(e)}\n{traceback.format_exc()}"
                log_debug("CRITICAL", "translate route 發生未捕捉錯誤", error_msg)
                self.log(f"❌ 嚴重錯誤: {str(e)}")
                return jsonify({"translatedText": q})

    def do_translate(self, text):
        self.translation_count += 1
        log_debug("INFO", f"do_translate 開始 (第{self.translation_count}次, 模式={self.mode})")
        
        # 檢查快取
        cache_key = (self.mode, self.api_url, self.model_name, text)
        if cache_key in self.cache:
            log_debug("INFO", "命中快取，直接回傳快取結果")
            return self.align_lyrics(text, self.cache[cache_key])

        translated = ""
        try:
            if self.mode == "Google":
                log_debug("INFO", "使用 Google 翻譯模式")
                translated = self.mode_backup_google(text)
            else:
                key = self.api_key.strip()
                if not key:
                    log_debug("WARN", "API Key 為空，降級到 Google 備援")
                    translated = self.mode_backup_google(text)
                elif self.mode == "Gemini":
                    log_debug("INFO", "使用 Gemini 模式")
                    translated = self._chunked_translate(text, key)
                else:
                    url = self.api_url.strip()
                    if not url:
                        log_debug("WARN", "API URL 為空，降級到 Google 備援")
                        translated = self.mode_backup_google(text)
                    elif "deepl.com" in url.lower():
                        log_debug("INFO", "使用 DeepL 模式")
                        translated = self._do_deepl(text, key, url)
                    else:
                        log_debug("INFO", f"使用 AI Chat 模式, url={url}, model={self.model_name}")
                        translated = self._chunked_translate(text, key, url)
        except Exception as e:
            error_msg = f"do_translate 錯誤: {str(e)}\n{traceback.format_exc()}"
            log_debug("ERROR", "翻譯過程發生例外", error_msg)
            self.log(f"❌ 翻譯錯誤: {str(e)}")
            # 最後防線：直接回傳原文
            return text
        
        # 存入快取
        self.cache[cache_key] = translated
        log_debug("INFO", f"翻譯完成，快取大小: {len(self.cache)}")
        
        # 強制進行行數對齊校正
        result = self.align_lyrics(text, translated)
        orig_lines = len(text.split('\n'))
        res_lines = len(result.split('\n'))
        if orig_lines != res_lines:
            log_debug("WARN", f"行數對齊後仍不符: 原文{orig_lines}行, 結果{res_lines}行")
        return result

    def _chunked_translate(self, text, key, url=None):
        """並行分段翻譯優化，保留完整行數結構"""
        lines = text.split('\n')
        log_debug("INFO", f"_chunked_translate: 共 {len(lines)} 行")
        
        # 24 行以下直接翻譯，減少 API 調用次數
        if len(lines) <= 24:
            log_debug("INFO", "行數<=24，直接翻譯不分段")
            if self.mode == "Gemini":
                return self._do_gemini(text, key)
            else:
                return self._do_ai_chat(text, key, url)

        # 超過 24 行則進行分段（用 12 行小 chunk，減少 timeout 風險）
        chunk_size = 12
        chunks = [lines[i:i + chunk_size] for i in range(0, len(lines), chunk_size)]
        chunk_texts = ['\n'.join(c) for c in chunks]
        results = [None] * len(chunks)
        log_debug("INFO", f"分段翻譯: {len(chunks)} 個 chunk, chunk_size={chunk_size}")

        def translate_one_chunk(idx):
            chunk_text = chunk_texts[idx]
            chunk_lines = chunk_text.split('\n')
            try:
                if self.mode == "Gemini":
                    translated = self._do_gemini(chunk_text, key)
                else:
                    translated = self._do_ai_chat(chunk_text, key, url)
                # ★ 關鍵修復：每個 chunk 獨立做 align_lyrics，確保行數完全對應
                # 這樣即使某個 chunk 回傳行數不符，也只影響該 chunk 本身，不會造成後面全部位移
                if translated:
                    aligned = self.align_lyrics(chunk_text, translated)
                    aligned_lines = aligned.split('\n')
                    if len(aligned_lines) == len(chunk_lines):
                        results[idx] = aligned
                        if len(translated.split('\n')) != len(chunk_lines):
                            log_debug("WARN", f"Chunk {idx+1}/{len(chunks)} 行數不符已修正 (期望{len(chunk_lines)}行, 得到{len(translated.split(chr(10)))}行→修正為{len(aligned_lines)}行)")
                        else:
                            log_debug("INFO", f"Chunk {idx+1}/{len(chunks)} 翻譯成功 ({len(chunk_lines)}行)")
                    else:
                        # 連 align_lyrics 後都還是不對，才退回原文
                        log_debug("WARN", f"Chunk {idx+1}/{len(chunks)} 修正後仍不符，退回原文")
                        results[idx] = chunk_text
                else:
                    results[idx] = chunk_text
            except Exception as e:
                log_debug("ERROR", f"Chunk {idx+1}/{len(chunks)} 拋出例外", f"{str(e)}\n{traceback.format_exc()}")
                # 例外時保留原文，align_lyrics 會處理
                results[idx] = chunk_text

        # 使用執行緒池同時發起請求 (最多 10 個並行，減少大歌排隊時間)
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            executor.map(translate_one_chunk, range(len(chunks)))

        # 組合結果（所有 chunk 都有結果，不會有 None）
        result = '\n'.join(results)
        log_debug("INFO", f"分段翻譯完成，組合後 {len(result.split(chr(10)))} 行")
        return result

    def _do_gemini(self, text, key):
        try:
            model = self.model_name.strip() or "gemini-1.5-flash"
            url = f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent?key={key}"
            payload = {
                "contents": [{
                    "parts": [{"text": self.system_prompt + "\n\n" + text}]
                }]
            }
            log_debug("INFO", f"Gemini API 請求: model={model}, input_lines={len(text.split(chr(10)))}")
            r = requests.post(url, json=payload, timeout=15)
            if r.status_code == 200:
                candidates = r.json().get('candidates', [])
                if candidates:
                    result = candidates[0]['content']['parts'][0]['text'].strip()
                    log_debug("INFO", f"Gemini API 成功: output_lines={len(result.split(chr(10)))}")
                    return result
            log_debug("WARN", f"Gemini API 失敗 HTTP {r.status_code}: {r.text[:200]}")
            return self.mode_backup_google(text)
        except Exception as e:
            log_debug("ERROR", f"Gemini API 拋出例外", f"{str(e)}\n{traceback.format_exc()}")
            return self.mode_backup_google(text)

    def _do_deepl(self, text, key, url):
        try:
            headers = {"Authorization": f"DeepL-Auth-Key {key}"}
            payload = {"text": [text], "target_lang": "ZH"}
            log_debug("INFO", f"DeepL API 請求: input_lines={len(text.split(chr(10)))}")
            r = requests.post(url, headers=headers, data=payload, timeout=10)
            if r.status_code == 200:
                result = r.json()['translations'][0]['text']
                log_debug("INFO", "DeepL API 成功")
                return result
            log_debug("WARN", f"DeepL API 失敗 HTTP {r.status_code}: {r.text[:200]}")
            return self.mode_backup_google(text)
        except Exception as e:
            log_debug("ERROR", f"DeepL API 拋出例外", f"{str(e)}\n{traceback.format_exc()}")
            return self.mode_backup_google(text)

    def _do_ai_chat(self, text, key, url):
        target_url = url
        if "chat/completions" not in target_url:
            if not target_url.endswith('/'): target_url += '/'
            if "v1" not in target_url: target_url += "v1/"
            target_url += "chat/completions"
        
        try:
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            payload = {
                "model": self.model_name,
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": text}
                ],
                "temperature": self.temperature
            }
            log_debug("INFO", f"AI Chat API 請求: target_url={target_url}, model={self.model_name}, input_lines={len(text.split(chr(10)))}")
            r = requests.post(target_url, headers=headers, json=payload, timeout=30)
            if r.status_code == 200:
                result = r.json()['choices'][0]['message']['content'].strip()
                log_debug("INFO", f"AI Chat API 成功: output_lines={len(result.split(chr(10)))}")
                return result
            log_debug("WARN", f"AI Chat API 失敗 HTTP {r.status_code}: {r.text[:200]}")
            return self.mode_backup_google(text)
        except Exception as e:
            log_debug("ERROR", f"AI Chat API 拋出例外", f"{str(e)}\n{traceback.format_exc()}")
            return self.mode_backup_google(text)

    def align_lyrics(self, original_text, translated_text):
        """
        嚴格對齊歌詞行數：強制讓回傳的行數與原文完全一致。
        任何行數不匹配都會被修正，確保每一行原文對應一行翻譯。
        """
        if not original_text: return ""
        
        # 將原文和譯文都按行拆分（保留空行）
        orig_lines = original_text.split('\n')
        trans_lines = translated_text.split('\n')
        
        orig_count = len(orig_lines)
        trans_count = len(trans_lines)
        if orig_count != trans_count:
            log_debug("DEBUG", f"align_lyrics 行數修正: 原文={orig_count}, 翻譯={trans_count}")
        
        # 確保翻譯行數與原文相同：太多就截斷，太少就補空行
        if len(trans_lines) > len(orig_lines):
            trans_lines = trans_lines[:len(orig_lines)]
        elif len(trans_lines) < len(orig_lines):
            trans_lines.extend([""] * (len(orig_lines) - len(trans_lines)))
        
        # 逐行對應，原文空行則保持空行
        result = []
        for i in range(len(orig_lines)):
            if not orig_lines[i].strip():
                # 原文是空行 → 輸出空行
                result.append("")
            else:
                # 原文有內容 → 輸出對應行的翻譯（或空白）
                line = trans_lines[i].strip() if i < len(trans_lines) else ""
                result.append(line)
        
        return "\n".join(result)

    def mode_backup_google(self, text):
        """
        使用 Google Translate API 批次翻譯，保留原始換行結構。
        一次請求翻譯整段文字，不會有逐行翻譯卡死的問題。
        """
        try:
            import urllib.parse
            # 將整段文字用 \n 分隔後一次送出，Google 會保留換行對齊
            url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=zh-TW&dt=t&q={urllib.parse.quote(text)}"
            log_debug("INFO", f"Google 翻譯請求: {len(text)} 字元")
            r = requests.get(url, timeout=15)
            if r.status_code == 200:
                parts = r.json()[0]
                translated = ""
                for part in parts:
                    if part and part[0]:
                        translated += part[0]
                
                # 確保換行結構與原文一致
                orig_lines = text.split('\n')
                trans_lines = translated.split('\n')
                
                # 行數不符時用 align_lyrics 補正
                if len(trans_lines) != len(orig_lines):
                    log_debug("WARN", f"Google 行數不符 (原文{len(orig_lines)}行, 翻譯{len(trans_lines)}行)，用 align_lyrics 修正")
                    return self.align_lyrics(text, translated)
                
                log_debug("INFO", f"Google 翻譯成功: {len(trans_lines)} 行")
                return translated
            else:
                log_debug("WARN", f"Google 翻譯 HTTP {r.status_code}: {r.text[:200]}")
                return text
        except Exception as e:
            log_debug("ERROR", f"Google 翻譯失敗", f"{str(e)}\n{traceback.format_exc()}")
            return text

    def test_connection(self):
        """測試 AI 配置是否有效，回傳 (成功與否, 訊息)"""
        log_debug("INFO", "開始測試連線")
        if self.mode == "Google":
            return False, "Google 模式無需測試"
        
        key = self.api_key.strip()
        if not key:
            log_debug("WARN", "測試連線: API Key 為空")
            return False, "API Key 為空"

        if self.mode == "Gemini":
            try:
                model = self.model_name.strip() or "gemini-1.5-flash"
                url = f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent?key={key}"
                payload = {
                    "contents": [{
                        "parts": [{"text": "Hello"}]
                    }]
                }
                log_debug("INFO", f"測試 Gemini 連線: model={model}")
                r = requests.post(url, json=payload, timeout=15)
                if r.status_code == 200:
                    candidates = r.json().get('candidates', [])
                    if candidates:
                        reply = candidates[0]['content']['parts'][0]['text'].strip()
                        log_debug("INFO", "Gemini 連線測試成功")
                        return True, f"✅ Gemini 連線成功！回應：{reply[:50]}"
                    log_debug("INFO", "Gemini 連線測試成功（無回應）")
                    return True, "✅ Gemini 連線成功（無回應內容）"
                else:
                    detail = ""
                    try:
                        detail = r.json().get('error', {}).get('message', r.text[:200])
                    except:
                        detail = r.text[:200]
                    log_debug("WARN", f"Gemini 連線測試失敗: {detail}")
                    return False, f"❌ Gemini 測試失敗 (HTTP {r.status_code})：{detail}"
            except Exception as e:
                log_debug("ERROR", f"Gemini 連線測試例外", f"{str(e)}\n{traceback.format_exc()}")
                return False, f"❌ Gemini 連線異常：{str(e)}"

        url = self.api_url.strip()
        if not url:
            log_debug("WARN", "測試連線: API URL 為空")
            return False, "API URL 為空"
        
        if "deepl.com" in url.lower():
            try:
                headers = {"Authorization": f"DeepL-Auth-Key {key}"}
                payload = {"text": ["Hello"], "target_lang": "ZH"}
                log_debug("INFO", "測試 DeepL 連線")
                r = requests.post(url, headers=headers, data=payload, timeout=10)
                if r.status_code == 200:
                    log_debug("INFO", "DeepL 連線測試成功")
                    return True, f"✅ DeepL 連線成功！回應：{r.json()['translations'][0]['text']}"
                else:
                    log_debug("WARN", f"DeepL 連線測試失敗 HTTP {r.status_code}")
                    return False, f"❌ DeepL 測試失敗 (HTTP {r.status_code})：{r.text[:100]}"
            except Exception as e:
                log_debug("ERROR", f"DeepL 連線測試例外", f"{str(e)}\n{traceback.format_exc()}")
                return False, f"❌ DeepL 連線異常：{str(e)}"
        else:
            target_url = url
            if "chat/completions" not in target_url:
                if not target_url.endswith('/'): target_url += '/'
                if "v1" not in target_url: target_url += "v1/"
                target_url += "chat/completions"
            
            try:
                headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
                payload = {
                    "model": self.model_name,
                    "messages": [
                        {"role": "system", "content": self.system_prompt},
                        {"role": "user", "content": "Hello"}
                    ],
                    "temperature": 0.4,
                    "max_tokens": 50
                }
                log_debug("INFO", f"測試 AI Chat 連線: {target_url}")
                r = requests.post(target_url, headers=headers, json=payload, timeout=15)
                if r.status_code == 200:
                    reply = r.json()['choices'][0]['message']['content'].strip()
                    log_debug("INFO", "AI Chat 連線測試成功")
                    return True, f"✅ AI 連線成功！回應：{reply[:50]}"
                else:
                    detail = ""
                    try:
                        detail = r.json().get('error', {}).get('message', r.text[:100])
                    except:
                        detail = r.text[:100]
                    log_debug("WARN", f"AI Chat 連線測試失敗 HTTP {r.status_code}: {detail}")
                    return False, f"❌ AI 測試失敗 (HTTP {r.status_code})：{detail}"
            except Exception as e:
                log_debug("ERROR", f"AI Chat 連線測試例外", f"{str(e)}\n{traceback.format_exc()}")
                return False, f"❌ AI 連線異常：{str(e)}"

    def run(self):
        import logging
        logging.getLogger('werkzeug').setLevel(logging.ERROR)
        log_debug("INFO", "Flask 伺服器開始監聽 http://127.0.0.1:5000")
        self.app.run(host='127.0.0.1', port=5000, debug=False, use_reloader=False, threaded=True)


class AppGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("BetterLyrics Ai歌詞翻譯助手 v2.5.2")
        self.root.geometry("550x810")
        self.profiles = {}
        self.server_started = False
        
        log_debug("INFO", "GUI 初始化開始")
        
        ttk.Label(root, text="BetterLyrics Ai歌詞翻譯助手 v2.5.2", font=('Arial', 12, 'bold')).pack(pady=10)
        
        manage_frame = ttk.LabelFrame(root, text=" 1. 選擇配置 ")
        manage_frame.pack(fill='x', padx=20, pady=5)
        self.profile_cb = ttk.Combobox(manage_frame, state="readonly")
        self.profile_cb.grid(row=0, column=0, padx=5, pady=5, sticky="ew")
        self.profile_cb.bind("<<ComboboxSelected>>", self.on_profile_change)
        self.btn_del = ttk.Button(manage_frame, text="刪除", command=self.delete_profile)
        self.btn_del.grid(row=0, column=1, padx=5, pady=5)
        manage_frame.columnconfigure(0, weight=1)

        self.edit_frame = ttk.LabelFrame(root, text=" 2. 編輯設定 ")
        self.edit_frame.pack(fill='x', padx=20, pady=5)
        ttk.Label(self.edit_frame, text="名稱:").grid(row=0, column=0, padx=5, pady=2, sticky="w")
        self.ent_name = ttk.Entry(self.edit_frame)
        self.ent_name.grid(row=0, column=1, padx=5, pady=2, sticky="ew")
        
        self.mode_var = tk.StringVar(value="Google")
        m_sub = ttk.Frame(self.edit_frame)
        m_sub.grid(row=1, column=1, sticky="w")
        ttk.Radiobutton(m_sub, text="Google", variable=self.mode_var, value="Google", command=self.update_ui).pack(side="left")
        ttk.Radiobutton(m_sub, text="AI / DeepL", variable=self.mode_var, value="AI", command=self.update_ui).pack(side="left")
        ttk.Radiobutton(m_sub, text="Gemini", variable=self.mode_var, value="Gemini", command=self.update_ui).pack(side="left")
        
        ttk.Label(self.edit_frame, text="API URL:").grid(row=2, column=0, padx=5, pady=2, sticky="w")
        self.ent_url = ttk.Entry(self.edit_frame)
        self.ent_url.grid(row=2, column=1, padx=5, pady=2, sticky="ew")
        
        ttk.Label(self.edit_frame, text="API Key:").grid(row=3, column=0, padx=5, pady=2, sticky="w")
        self.ent_key = ttk.Entry(self.edit_frame, show="*")
        self.ent_key.grid(row=3, column=1, padx=5, pady=2, sticky="ew")
        
        ttk.Label(self.edit_frame, text="AI 模型:").grid(row=4, column=0, padx=5, pady=2, sticky="w")
        self.ent_model = ttk.Entry(self.edit_frame)
        self.ent_model.grid(row=4, column=1, padx=5, pady=2, sticky="ew")
        
        ttk.Label(self.edit_frame, text="提示詞:").grid(row=5, column=0, padx=5, pady=2, sticky="nw")
        self.txt_prompt = tk.Text(self.edit_frame, height=4, width=30)
        self.txt_prompt.grid(row=5, column=1, padx=5, pady=2, sticky="ew")
        
        ttk.Label(self.edit_frame, text="創造力 (Temperature):").grid(row=6, column=0, padx=5, pady=2, sticky="w")
        temp_frame = ttk.Frame(self.edit_frame)
        temp_frame.grid(row=6, column=1, padx=5, pady=2, sticky="ew")
        self.temp_var = tk.DoubleVar(value=0.1)
        self.temp_scale = ttk.Scale(temp_frame, from_=0.0, to=1.0, orient="horizontal", variable=self.temp_var, command=self._on_temp_change)
        self.temp_scale.pack(side="left", fill="x", expand=True)
        self.temp_label = ttk.Label(temp_frame, text="0.10", width=5)
        self.temp_label.pack(side="right", padx=(5,0))
        
        btn_frame = ttk.Frame(self.edit_frame)
        btn_frame.grid(row=7, column=0, columnspan=2, pady=10)
        ttk.Button(btn_frame, text="儲存配置", command=self.save_current_profile).pack(side="left", padx=5)
        self.btn_test = ttk.Button(btn_frame, text="測試連線", command=self.test_ai_config)
        self.btn_test.pack(side="left", padx=5)
        self.edit_frame.columnconfigure(1, weight=1)

        # 為所有 Entry 和 Text 欄位加上右鍵選單
        self._add_right_click_menu(self.ent_name)
        self._add_right_click_menu(self.ent_url)
        self._add_right_click_menu(self.ent_key)
        self._add_right_click_menu(self.ent_model)
        self._add_right_click_menu_text(self.txt_prompt)

        self.log_area = scrolledtext.ScrolledText(root, height=10, font=('Consolas', 9))
        self.log_area.pack(fill='both', padx=20, pady=5, expand=True)

        ctrl_frame = ttk.Frame(root)
        ctrl_frame.pack(pady=5)
        self.btn_start = ttk.Button(ctrl_frame, text="啟動伺服器", command=self.start_server)
        self.btn_start.pack(side="left", padx=5)
        self.btn_stop = ttk.Button(ctrl_frame, text="停止伺服器", command=self.stop_server, state="disabled")
        self.btn_stop.pack(side="left", padx=5)
        # Debug 日誌按鈕
        self.btn_debug = ttk.Button(ctrl_frame, text="📋 Debug Log", command=self.open_debug_log)
        self.btn_debug.pack(side="left", padx=5)

        self.server = TranslationServer(self.log_area)
        self.load_all_profiles()
        self.update_ui()
        log_debug("INFO", "GUI 初始化完成")

    def open_debug_log(self):
        """用記事本開啟 Debug 日誌檔案"""
        try:
            if os.path.exists(DEBUG_LOG_FILE):
                log_debug("INFO", f"使用者開啟 Debug Log: {os.path.abspath(DEBUG_LOG_FILE)}")
                os.startfile(os.path.abspath(DEBUG_LOG_FILE))
            else:
                self.server.log("⚠️ Debug 日誌檔案不存在，尚未有任何記錄")
        except Exception as e:
            self.server.log(f"❌ 無法開啟 Debug Log: {str(e)}")

    def update_ui(self):
        """根據模式決定 Entry 是否可編輯"""
        mode = self.mode_var.get()
        if mode == "Google":
            s_ai = 'disabled'
            s_gemini = 'disabled'
        elif mode == "Gemini":
            s_ai = 'disabled'
            s_gemini = 'normal'
        else:  # AI
            s_ai = 'normal'
            s_gemini = 'disabled'
        
        # AI 模式下 API URL 和 Key 可編輯；Gemini 模式 Key/模型/提示詞可編輯
        self.ent_url.config(state=s_ai)
        self.ent_key.config(state='normal' if mode != "Google" else 'disabled')
        self.ent_model.config(state='normal' if mode != "Google" else 'disabled')
        self.txt_prompt.config(state='normal' if mode != "Google" else 'disabled')
        self.btn_test.config(state='normal' if mode != "Google" else 'disabled')

    def _on_temp_change(self, event=None):
        """溫度滑桿變動時，更新顯示數字"""
        val = self.temp_var.get()
        self.temp_label.config(text=f"{val:.2f}")

    def load_all_profiles(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    self.profiles = json.load(f)
            except Exception as e:
                log_debug("ERROR", f"讀取設定檔失敗", f"{str(e)}\n{traceback.format_exc()}")
                self.profiles = {}
        if not self.profiles:
            self.profiles = {"預設 Google": {"mode": "Google", "url": "", "key": ""}}
        self.profile_cb['values'] = list(self.profiles.keys())
        self.profile_cb.set(list(self.profiles.keys())[0])
        self.on_profile_change(None)

    def on_profile_change(self, event):
        """切換配置時的核心修復邏輯"""
        name = self.profile_cb.get()
        p = self.profiles.get(name)
        if not p: return

        # 1. 先強行將狀態設為 normal，否則無法填入內容
        self.ent_url.config(state='normal')
        self.ent_key.config(state='normal')
        self.ent_model.config(state='normal')
        self.txt_prompt.config(state='normal')

        # 2. 清除並填入新值
        self.ent_name.delete(0, tk.END)
        self.ent_name.insert(0, name)
        
        self.mode_var.set(p.get("mode", "Google"))
        
        self.ent_url.delete(0, tk.END)
        self.ent_url.insert(0, p.get("url", ""))
        
        self.ent_key.delete(0, tk.END)
        self.ent_key.insert(0, p.get("key", ""))
        
        self.ent_model.delete(0, tk.END)
        self.ent_model.insert(0, p.get("model", "gpt-3.5-turbo"))
        
        self.txt_prompt.delete("1.0", tk.END)
        self.txt_prompt.insert("1.0", p.get("prompt", "你是一位專業的歌詞翻譯家，將歌詞翻譯成「台灣繁體」。嚴禁簡體，使用台灣慣用語。請嚴格保持輸入的行數和順序，每行一對一翻譯，不要合併或拆分任何行。"))
        
        # 讀取創造力 (Temperature)
        temp = p.get("temperature", 0.1)
        self.temp_var.set(temp)
        self.temp_label.config(text=f"{temp:.2f}")
        
        # 3. 填完後再根據該配置的 mode 執行 UI 鎖定/解鎖
        self.update_ui()

    def save_current_profile(self):
        name = self.ent_name.get().strip()
        if not name: return
        self.profiles[name] = {
            "mode": self.mode_var.get(), 
            "url": self.ent_url.get(), 
            "key": self.ent_key.get(),
            "model": self.ent_model.get(),
            "prompt": self.txt_prompt.get("1.0", tk.END).strip(),
            "temperature": self.temp_var.get()
        }
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.profiles, f, ensure_ascii=False)
            self.profile_cb['values'] = list(self.profiles.keys())
            self.profile_cb.set(name)
            self.server.log(f"配置 '{name}' 已儲存")
            log_debug("INFO", f"配置 '{name}' 已儲存")
        except Exception as e:
            log_debug("ERROR", f"儲存設定檔失敗", f"{str(e)}\n{traceback.format_exc()}")
            self.server.log(f"❌ 儲存失敗: {str(e)}")

    def delete_profile(self):
        name = self.profile_cb.get()
        if name in self.profiles and len(self.profiles) > 1:
            del self.profiles[name]
            try:
                with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                    json.dump(self.profiles, f, ensure_ascii=False)
                log_debug("INFO", f"刪除配置 '{name}'")
            except Exception as e:
                log_debug("ERROR", f"刪除設定檔失敗", f"{str(e)}\n{traceback.format_exc()}")
            self.load_all_profiles()

    def start_server(self):
        self.server.mode = self.mode_var.get()
        self.server.api_key = self.ent_key.get()
        self.server.api_url = self.ent_url.get()
        self.server.model_name = self.ent_model.get()
        self.server.system_prompt = self.txt_prompt.get("1.0", tk.END).strip()
        self.server.temperature = self.temp_var.get()
        self.server.is_active = True
        self.btn_start.config(state='disabled')
        self.btn_stop.config(state='normal')
        if not self.server_started:
            self.server_started = True
            threading.Thread(target=self.server.run, daemon=True).start()
        
        msg = f">>> 伺服器啟動！模式: {self.server.mode}"
        self.server.log(msg)
        log_debug("INFO", msg)
        log_debug("INFO", f"伺服器設定 - mode={self.server.mode}, url={self.server.api_url}, model={self.server.model_name}")

    def test_ai_config(self):
        """測試當前 AI 配置是否有效（在背景執行緒執行）"""
        # 先將 UI 當前值同步到 server 物件
        self.server.mode = self.mode_var.get()
        self.server.api_key = self.ent_key.get()
        self.server.api_url = self.ent_url.get()
        self.server.model_name = self.ent_model.get()
        self.server.system_prompt = self.txt_prompt.get("1.0", tk.END).strip()
        
        self.server.log("⏳ 正在測試連線，請稍候...")
        self.btn_test.config(state='disabled', text='測試中...')
        
        def _do_test():
            try:
                success, msg = self.server.test_connection()
                # 回到主執行緒更新 UI
                self.root.after(0, lambda: self._test_done(success, msg))
            except Exception as e:
                log_debug("ERROR", "測試連線背景執行緒錯誤", f"{str(e)}\n{traceback.format_exc()}")
                self.root.after(0, lambda: self._test_done(False, f"❌ 測試連線發生錯誤: {str(e)}"))
        
        threading.Thread(target=_do_test, daemon=True).start()

    def _test_done(self, success, msg):
        """測試完成後的回呼"""
        self.server.log(msg)
        self.btn_test.config(state='normal', text='測試連線')
        # 根據模式重新決定按鈕狀態
        self.update_ui()

    def stop_server(self):
        self.server.is_active = False
        self.btn_start.config(state='normal')
        self.btn_stop.config(state='disabled')
        msg = ">>> 伺服器已暫停"
        self.server.log(msg)
        log_debug("INFO", msg)

    def _add_right_click_menu(self, entry):
        """為 ttk.Entry 加上右鍵選單（複製、貼上、清除）"""
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="複製", command=lambda: self._entry_copy(entry))
        menu.add_command(label="貼上", command=lambda: self._entry_paste(entry))
        menu.add_separator()
        menu.add_command(label="清除", command=lambda: self._entry_clear(entry))
        entry.bind("<Button-3>", lambda e: self._show_menu(menu, e))

    def _add_right_click_menu_text(self, text_widget):
        """為 tk.Text 加上右鍵選單（複製、貼上、清除）"""
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="複製", command=lambda: self._text_copy(text_widget))
        menu.add_command(label="貼上", command=lambda: self._text_paste(text_widget))
        menu.add_separator()
        menu.add_command(label="清除", command=lambda: self._text_clear(text_widget))
        text_widget.bind("<Button-3>", lambda e: self._show_menu(menu, e))

    def _show_menu(self, menu, event):
        """顯示右鍵選單"""
        menu.tk_popup(event.x_root, event.y_root)

    def _entry_copy(self, entry):
        """複製 Entry 選取文字"""
        try:
            self.root.clipboard_clear()
            text = entry.selection_get()
            self.root.clipboard_append(text)
        except tk.TclError:
            pass

    def _entry_paste(self, entry):
        """貼上到 Entry"""
        try:
            text = self.root.clipboard_get()
            entry.insert(tk.INSERT, text)
        except tk.TclError:
            pass

    def _entry_clear(self, entry):
        """清除 Entry 內容"""
        entry.delete(0, tk.END)

    def _text_copy(self, text_widget):
        """複製 Text 選取文字"""
        try:
            self.root.clipboard_clear()
            text = text_widget.selection_get()
            self.root.clipboard_append(text)
        except tk.TclError:
            pass

    def _text_paste(self, text_widget):
        """貼上到 Text"""
        try:
            text = self.root.clipboard_get()
            text_widget.insert(tk.INSERT, text)
        except tk.TclError:
            pass

    def _text_clear(self, text_widget):
        """清除 Text 內容"""
        text_widget.delete("1.0", tk.END)


if __name__ == '__main__':
    log_debug("INFO", "程式啟動")
    try:
        root = tk.Tk()
        AppGUI(root)
        root.mainloop()
    except Exception as e:
        log_debug("CRITICAL", "主程式發生未捕捉錯誤", f"{str(e)}\n{traceback.format_exc()}")
    log_debug("INFO", "程式結束")
