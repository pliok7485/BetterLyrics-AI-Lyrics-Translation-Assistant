import threading
import requests
import json
import os
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
        self.is_active = False 
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

        @self.app.route('/languages', methods=['GET'])
        def languages():
            return jsonify([{"code": "zh", "name": "Chinese"}])

    def do_translate(self, text):
        if self.mode == "Google":
            return self.mode_backup_google(text)
        
        url = self.api_url.strip()
        key = self.api_key.strip()
        if not url or not key: return self.mode_backup_google(text)

        # --- DeepL 模式 (DeepL 不支援台灣繁體參數，效果仍可能夾雜簡體) ---
        if "deepl.com" in url.lower():
            try:
                headers = {"Authorization": f"DeepL-Auth-Key {key}"}
                payload = {"text": [text], "target_lang": "ZH"}
                r = requests.post(url, headers=headers, data=payload, timeout=10)
                return r.json()['translations'][0]['text'] if r.status_code == 200 else self.mode_backup_google(text)
            except: return self.mode_backup_google(text)

        # --- AI 模式 (DeepSeek / GPT): 強化台灣繁體指令 ---
        else:
            target_url = url
            if "chat/completions" not in target_url:
                if not target_url.endswith('/'): target_url += '/'
                if "v1" not in target_url: target_url += "v1/"
                target_url += "chat/completions"
            
            try:
                headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
                
                # 這裡是最核心的指令修改
                payload = {
                    "model": "deepseek-chat" if "deepseek" in target_url else "gpt-3.5-turbo",
                    "messages": [
                        {
                            "role": "system", 
                            "content": (
                                "你是一位專業的歌詞翻譯家，精通台灣繁體中文。請將輸入的歌詞翻譯成「台灣繁體」。\n"
                                "指令規範：\n"
                                "1. 嚴禁使用簡體字。\n"
                                "2. 使用台灣在地慣用語（例如：品質、影片、軟體、訊息）。\n"
                                "3. 保持歌詞的意境與押韻感。\n"
                                "4. 直接輸出譯文，不要有任何解釋或贅字。"
                            )
                        },
                        {"role": "user", "content": text}
                    ],
                    "temperature": 0.4 # 稍微提高一點溫度讓歌詞更自然
                }
                
                r = requests.post(target_url, headers=headers, json=payload, timeout=15)
                if r.status_code == 200:
                    return r.json()['choices'][0]['message']['content'].strip()
                else:
                    self.log(f"!!! AI 錯誤 ({r.status_code})")
                    return self.mode_backup_google(text)
            except Exception as e:
                self.log(f"!!! AI 連線異常: {str(e)[:50]}")
                return self.mode_backup_google(text)

    def mode_backup_google(self, text):
        try:
            url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=zh-TW&dt=t&q={text}"
            r = requests.get(url, timeout=5)
            return "".join([l[0] for l in r.json()[0] if l and l[0]])
        except: return text

    def run(self):
        import logging
        logging.getLogger('werkzeug').setLevel(logging.ERROR)
        self.app.run(host='127.0.0.1', port=5000, debug=False, use_reloader=False)

class AppGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("BetterLyrics 助手 v2.3 (台語化強化版)")
        self.root.geometry("550x700")
        self.profiles = {}
        self.server_started = False
        
        ttk.Label(root, text="歌詞中轉伺服器 v2.3", font=('Arial', 12, 'bold')).pack(pady=10)
        
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
        
        ttk.Label(self.edit_frame, text="API URL:").grid(row=2, column=0, padx=5, pady=2, sticky="w")
        self.ent_url = ttk.Entry(self.edit_frame)
        self.ent_url.grid(row=2, column=1, padx=5, pady=2, sticky="ew")
        
        ttk.Label(self.edit_frame, text="API Key:").grid(row=3, column=0, padx=5, pady=2, sticky="w")
        self.ent_key = ttk.Entry(self.edit_frame, show="*")
        self.ent_key.grid(row=3, column=1, padx=5, pady=2, sticky="ew")
        
        ttk.Button(self.edit_frame, text="儲存配置", command=self.save_current_profile).grid(row=4, column=0, columnspan=2, pady=10)
        self.edit_frame.columnconfigure(1, weight=1)

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
        s = 'normal' if self.mode_var.get() == "AI" else 'disabled'
        self.ent_url.config(state=s); self.ent_key.config(state=s)

    def load_all_profiles(self):
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                self.profiles = json.load(f)
        if not self.profiles:
            self.profiles = {"預設 Google": {"mode": "Google", "url": "", "key": ""}}
        self.profile_cb['values'] = list(self.profiles.keys()); self.profile_cb.set(list(self.profiles.keys())[0])
        self.on_profile_change(None)

    def on_profile_change(self, event):
        name = self.profile_cb.get()
        p = self.profiles.get(name, {})
        self.ent_name.delete(0, tk.END); self.ent_name.insert(0, name)
        self.mode_var.set(p.get("mode", "Google"))
        self.ent_url.delete(0, tk.END); self.ent_url.insert(0, p.get("url", ""))
        self.ent_key.delete(0, tk.END); self.ent_key.insert(0, p.get("key", ""))
        self.update_ui()

    def save_current_profile(self):
        name = self.ent_name.get().strip()
        if not name: return
        self.profiles[name] = {"mode": self.mode_var.get(), "url": self.ent_url.get(), "key": self.ent_key.get()}
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.profiles, f, ensure_ascii=False)
        self.profile_cb['values'] = list(self.keys()); self.profile_cb.set(name)
        self.server.log(f"配置 '{name}' 已儲存")

    def delete_profile(self):
        name = self.profile_cb.get()
        if name in self.profiles and len(self.profiles) > 1:
            del self.profiles[name]
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.profiles, f, ensure_ascii=False)
            self.load_all_profiles()

    def start_server(self):
        self.server.mode, self.server.api_key, self.server.api_url = self.mode_var.get(), self.ent_key.get(), self.ent_url.get()
        self.server.is_active = True
        self.btn_start.config(state='disabled'); self.btn_stop.config(state='normal')
        if not self.server_started:
            self.server_started = True
            threading.Thread(target=self.server.run, daemon=True).start()
        self.server.log(f">>> 伺服器啟動！目前模式: {self.server.mode}")

    def stop_server(self):
        self.server.is_active = False
        self.btn_start.config(state='normal'); self.btn_stop.config(state='disabled')
        self.server.log(">>> 伺服器已暫停")

if __name__ == '__main__':
    root = tk.Tk()
    AppGUI(root)
    root.mainloop()
