import threading
import requests
import json
import os
import concurrent.futures
from flask import Flask, request, jsonify
import tkinter as tk
from tkinter import scrolledtext, ttk

CONFIG_FILE = "betterlyrics_profiles.json"

class TranslationServer:
    def __init__(self, log_widget):
        self.log_widget = log_widget
        self.app = Flask(__name__)
        self.mode = "Google"
        self.api_key = ""
        self.api_url = ""
        self.model_name = "gpt-3.5-turbo"
        self.system_prompt = "你是一位專業的歌詞翻譯家，將歌詞翻譯成「台灣繁體」。嚴禁簡體，使用台灣慣用語。\n\n【嚴格規則】\n1. 行數必須完全一致：輸入有 N 行，輸出也必須有 N 行。\n2. 第一行翻譯對應第一行，第二行對應第二行，依此類推。\n3. 空行保持為空行。\n4. 每行一對一翻譯，絕對不要合併或拆分任何行。\n5. 不要添加任何說明文字，只輸出翻譯結果。"
        self.is_active = False
        self.cache = {}  # 翻譯快取：{ (mode, text): translated_text }
        self.setup_routes()

    def log(self, message):
        self.log_widget.insert(tk.END, f"{message}\n")
        self.log_widget.see(tk.END)

    def setup_routes(self):
        @self.app.route('/translate', methods=['POST', 'GET'])
        @self.app.route('/api/v1/translate', methods=['POST', 'GET'])
        def translate():
            if not self.is_active:
                return jsonify({"error": "Server stopped"}), 503
            data = request.get_json(force=True, silent=True) or {}
            q = data.get('q') or request.form.get('q') or request.args.get('q', '')
            if not q: return jsonify({"translatedText": ""})
            return jsonify({"translatedText": self.do_translate(q)})

    def do_translate(self, text):
        # 1. 記錄原文行數結構
        orig_line_count = len(text.split('\n'))
        
        # 2. 檢查快取
        cache_key = (self.mode, self.api_url, self.model_name, text)
        if cache_key in self.cache:
            return self.align_lyrics(text, self.cache[cache_key])

        translated = ""
        if self.mode == "Google":
            translated = self.mode_backup_google(text)
        else:
            key = self.api_key.strip()
            if not key: 
                translated = self.mode_backup_google(text)
            elif self.mode == "Gemini":
                translated = self._chunked_translate(text, key)
            else:
                url = self.api_url.strip()
                if not url:
                    translated = self.mode_backup_google(text)
                elif "deepl.com" in url.lower():
                    # DeepL 本身速度極快，不需要分段
                    translated = self._do_deepl(text, key, url)
                else:
                    # AI 聊天模式採用分段加速
                    translated = self._chunked_translate(text, key, url)
        
        # 3. 存入快取
        self.cache[cache_key] = translated
        
        # 4. 強制進行行數對齊校正（嚴格保持行數一致）
        return self.align_lyrics(text, translated)

    def _chunked_translate(self, text, key, url=None):
        """並行分段翻譯優化，保留完整行數結構"""
        lines = text.split('\n')
        # 如果行數很少，直接翻譯即可
        if len(lines) <= 12:
            if self.mode == "Gemini":
                return self._do_gemini(text, key)
            else:
                return self._do_ai_chat(text, key, url)

        # 超過 12 行則進行分段
        chunk_size = 12
        chunks = [lines[i:i + chunk_size] for i in range(0, len(lines), chunk_size)]
        chunk_texts = ['\n'.join(c) for c in chunks]
        results = [None] * len(chunks)

        def translate_one_chunk(idx):
            chunk_text = chunk_texts[idx]
            chunk_lines = chunk_text.split('\n')
            if self.mode == "Gemini":
                translated = self._do_gemini(chunk_text, key)
            else:
                translated = self._do_ai_chat(chunk_text, key, url)
            # 確保 chunk 的行數一致
            if translated and len(translated.split('\n')) == len(chunk_lines):
                results[idx] = translated
            else:
                # chunk 失敗或行數不匹配 → 用每行的 Google 備援（保留行數）
                fallback_lines = []
                for cl in chunk_lines:
                    if cl.strip():
                        fallback_lines.append(self.mode_backup_google(cl))
                    else:
                        fallback_lines.append("")
                results[idx] = '\n'.join(fallback_lines)

        # 使用執行緒池同時發起請求 (最多 5 個並行)
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            executor.map(translate_one_chunk, range(len(chunks)))

        # 組合結果（所有 chunk 都有結果，不會有 None）
        return '\n'.join(results)

    def _do_gemini(self, text, key):
        try:
            model = self.model_name.strip() or "gemini-1.5-flash"
            url = f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent?key={key}"
            payload = {
                "contents": [{
                    "parts": [{"text": self.system_prompt + "\n\n" + text}]
                }]
            }
            r = requests.post(url, json=payload, timeout=15)
            if r.status_code == 200:
                candidates = r.json().get('candidates', [])
                if candidates:
                    return candidates[0]['content']['parts'][0]['text'].strip()
            return self.mode_backup_google(text)
        except:
            return self.mode_backup_google(text)

    def _do_deepl(self, text, key, url):
        try:
            headers = {"Authorization": f"DeepL-Auth-Key {key}"}
            payload = {"text": [text], "target_lang": "ZH"}
            r = requests.post(url, headers=headers, data=payload, timeout=10)
            if r.status_code == 200:
                return r.json()['translations'][0]['text']
            return self.mode_backup_google(text)
        except: return self.mode_backup_google(text)

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
                "temperature": 0.4
            }
            r = requests.post(target_url, headers=headers, json=payload, timeout=15)
            if r.status_code == 200:
                return r.json()['choices'][0]['message']['content'].strip()
            return self.mode_backup_google(text)
        except: return self.mode_backup_google(text)

    def align_lyrics(self, original_text, translated_text):
        """
        嚴格對齊歌詞行數：強制讓回傳的行數與原文完全一致。
        任何行數不匹配都會被修正，確保每一行原文對應一行翻譯。
        """
        if not original_text: return ""
        
        # 將原文和譯文都按行拆分（保留空行）
        orig_lines = original_text.split('\n')
        trans_lines = translated_text.split('\n')
        
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
        """逐行翻譯，嚴格保留換行結構"""
        try:
            lines = text.split('\n')
            result_lines = []
            for line in lines:
                if line.strip():
                    url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=zh-TW&dt=t&q={line}"
                    r = requests.get(url, timeout=5)
                    parts = r.json()[0]
                    translated = ""
                    for part in parts:
                        if part and part[0]:
                            translated += part[0]
                    result_lines.append(translated.strip())
                else:
                    result_lines.append("")
            return "\n".join(result_lines)
        except: return text

    def test_connection(self):
        """測試 AI 配置是否有效，回傳 (成功與否, 訊息)"""
        if self.mode == "Google":
            return False, "Google 模式無需測試"
        
        key = self.api_key.strip()
        if not key:
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
                r = requests.post(url, json=payload, timeout=15)
                if r.status_code == 200:
                    candidates = r.json().get('candidates', [])
                    if candidates:
                        reply = candidates[0]['content']['parts'][0]['text'].strip()
                        return True, f"✅ Gemini 連線成功！回應：{reply[:50]}"
                    return True, "✅ Gemini 連線成功（無回應內容）"
                else:
                    detail = ""
                    try:
                        detail = r.json().get('error', {}).get('message', r.text[:200])
                    except:
                        detail = r.text[:200]
                    return False, f"❌ Gemini 測試失敗 (HTTP {r.status_code})：{detail}"
            except Exception as e:
                return False, f"❌ Gemini 連線異常：{str(e)}"

        url = self.api_url.strip()
        if not url:
            return False, "API URL 為空"
        
        if "deepl.com" in url.lower():
            try:
                headers = {"Authorization": f"DeepL-Auth-Key {key}"}
                payload = {"text": ["Hello"], "target_lang": "ZH"}
                r = requests.post(url, headers=headers, data=payload, timeout=10)
                if r.status_code == 200:
                    return True, f"✅ DeepL 連線成功！回應：{r.json()['translations'][0]['text']}"
                else:
                    return False, f"❌ DeepL 測試失敗 (HTTP {r.status_code})：{r.text[:100]}"
            except Exception as e:
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
                r = requests.post(target_url, headers=headers, json=payload, timeout=15)
                if r.status_code == 200:
                    reply = r.json()['choices'][0]['message']['content'].strip()
                    return True, f"✅ AI 連線成功！回應：{reply[:50]}"
                else:
                    detail = ""
                    try:
                        detail = r.json().get('error', {}).get('message', r.text[:100])
                    except:
                        detail = r.text[:100]
                    return False, f"❌ AI 測試失敗 (HTTP {r.status_code})：{detail}"
            except Exception as e:
                return False, f"❌ AI 連線異常：{str(e)}"

    def run(self):
        import logging
        logging.getLogger('werkzeug').setLevel(logging.ERROR)
        self.app.run(host='127.0.0.1', port=5000, debug=False, use_reloader=False, threaded=True)

class AppGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("BetterLyrics 助手 v2.5.1")
        self.root.geometry("550x780")
        self.profiles = {}
        self.server_started = False
        
        ttk.Label(root, text="歌詞中轉伺服器 v2.5.1", font=('Arial', 12, 'bold')).pack(pady=10)
        
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
        
        btn_frame = ttk.Frame(self.edit_frame)
        btn_frame.grid(row=6, column=0, columnspan=2, pady=10)
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

        self.log_area = scrolledtext.ScrolledText(root, height=12, font=('Consolas', 9))
        self.log_area.pack(fill='both', padx=20, pady=5, expand=True)

        ctrl_frame = ttk.Frame(root)
        ctrl_frame.pack(pady=15)
        self.btn_start = ttk.Button(ctrl_frame, text="啟動伺服器", command=self.start_server)
        self.btn_start.pack(side="left", padx=10)
        self.btn_stop = ttk.Button(ctrl_frame, text="停止伺服器", command=self.stop_server, state="disabled")
        self.btn_stop.pack(side="left", padx=10)

        self.server = TranslationServer(self.log_area)
        self.load_all_profiles()
        self.update_ui()

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

    def load_all_profiles(self):
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                self.profiles = json.load(f)
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
            "prompt": self.txt_prompt.get("1.0", tk.END).strip()
        }
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.profiles, f, ensure_ascii=False)
        self.profile_cb['values'] = list(self.profiles.keys())
        self.profile_cb.set(name)
        self.server.log(f"配置 '{name}' 已儲存")

    def delete_profile(self):
        name = self.profile_cb.get()
        if name in self.profiles and len(self.profiles) > 1:
            del self.profiles[name]
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.profiles, f, ensure_ascii=False)
            self.load_all_profiles()

    def start_server(self):
        self.server.mode = self.mode_var.get()
        self.server.api_key = self.ent_key.get()
        self.server.api_url = self.ent_url.get()
        self.server.model_name = self.ent_model.get()
        self.server.system_prompt = self.txt_prompt.get("1.0", tk.END).strip()
        self.server.is_active = True
        self.btn_start.config(state='disabled')
        self.btn_stop.config(state='normal')
        if not self.server_started:
            self.server_started = True
            threading.Thread(target=self.server.run, daemon=True).start()
        self.server.log(f">>> 伺服器啟動！模式: {self.server.mode}")

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
            success, msg = self.server.test_connection()
            # 回到主執行緒更新 UI
            self.root.after(0, lambda: self._test_done(success, msg))
        
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
        self.server.log(">>> 伺服器已暫停")

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
    root = tk.Tk()
    AppGUI(root)
    root.mainloop()
