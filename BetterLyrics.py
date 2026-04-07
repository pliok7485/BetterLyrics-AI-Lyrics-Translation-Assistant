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

    def do_translate(self, text):
        if self.mode == "Google":
            return self.mode_backup_google(text)
        
        url = self.api_url.strip()
        key = self.api_key.strip()
        if not url or not key: return self.mode_backup_google(text)

        if "deepl.com" in url.lower():
            try:
                headers = {"Authorization": f"DeepL-Auth-Key {key}"}
                payload = {"text": [text], "target_lang": "ZH"}
                r = requests.post(url, headers=headers, data=payload, timeout=10)
                return r.json()['translations'][0]['text'] if r.status_code == 200 else self.mode_backup_google(text)
            except: return self.mode_backup_google(text)
        else:
            target_url = url
            if "chat/completions" not in target_url:
                if not target_url.endswith('/'): target_url += '/'
                if "v1" not in target_url: target_url += "v1/"
                target_url += "chat/completions"
            
            try:
                headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
                payload = {
                    "model": "deepseek-chat" if "deepseek" in target_url else "gpt-3.5-turbo",
                    "messages": [
                        {
                            "role": "system", 
                            "content": "你是一位專業的歌詞翻譯家，將歌詞翻譯成「台灣繁體」。嚴禁簡體，使用台灣慣用語。"
                        },
                        {"role": "user", "content": text}
                    ],
                    "temperature": 0.4
                }
                r = requests.post(target_url, headers=headers, json=payload, timeout=15)
                return r.json()['choices'][0]['message']['content'].strip() if r.status_code == 200 else self.mode_backup_google(text)
            except: return self.mode_backup_google(text)

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
        self.root.title("BetterLyrics 助手 v2.4")
        self.root.geometry("550x700")
        self.profiles = {}
        self.server_started = False
        
        ttk.Label(root, text="歌詞中轉伺服器 v2.4", font=('Arial', 12, 'bold')).pack(pady=10)
        
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
        """根據模式決定 Entry 是否可編輯"""
        s = 'normal' if self.mode_var.get() == "AI" else 'disabled'
        self.ent_url.config(state=s)
        self.ent_key.config(state=s)

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

        # 2. 清除並填入新值
        self.ent_name.delete(0, tk.END)
        self.ent_name.insert(0, name)
        
        self.mode_var.set(p.get("mode", "Google"))
        
        self.ent_url.delete(0, tk.END)
        self.ent_url.insert(0, p.get("url", ""))
        
        self.ent_key.delete(0, tk.END)
        self.ent_key.insert(0, p.get("key", ""))
        
        # 3. 填完後再根據該配置的 mode 執行 UI 鎖定/解鎖
        self.update_ui()

    def save_current_profile(self):
        name = self.ent_name.get().strip()
        if not name: return
        self.profiles[name] = {
            "mode": self.mode_var.get(), 
            "url": self.ent_url.get(), 
            "key": self.ent_key.get()
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
        self.server.is_active = True
        self.btn_start.config(state='disabled')
        self.btn_stop.config(state='normal')
        if not self.server_started:
            self.server_started = True
            threading.Thread(target=self.server.run, daemon=True).start()
        self.server.log(f">>> 伺服器啟動！模式: {self.server.mode}")

    def stop_server(self):
        self.server.is_active = False
        self.btn_start.config(state='normal')
        self.btn_stop.config(state='disabled')
        self.server.log(">>> 伺服器已暫停")

if __name__ == '__main__':
    root = tk.Tk()
    AppGUI(root)
    root.mainloop()
