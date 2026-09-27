import json
import os
import subprocess
import webbrowser
import tkinter as tk
from tkinter import scrolledtext, messagebox
import keyboard

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
DEFAULT_CONFIG = {
    "mappings": {
        "timesheet": "https://example.com/timesheet",
        "idea": ""
    },
    "directories": {},
    "websites": {}
}

SHOW_HOTKEY = "alt+space"

class Launcher:
    def __init__(self):
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes('-alpha', 0.95)
        self.root.attributes('-topmost', True)
        self.root.configure(bg='#1e1e1e')
        self.root.geometry('320x400+100+100')
        
        self.shown = False
        self.config = {}
        self.keywords = []
        
        self.setup_window()
        self.load_config()
        self.setup_hotkey()
        
        self.root.mainloop()
    
    def setup_window(self):
        self.entry = tk.Entry(self.root, font=("SF Meta", 14), 
                           fg='#ffffff', bg='#1e1e1e',
                           insertbackground='white',
                           relief=tk.FLAT, selectbackground='#09c',
                           selectforeground='#ffffff')
        self.entry.pack(fill=tk.X, padx=10, pady=8)
        self.entry.bind('<KeyRelease>', self.on_key_release)
        self.entry.bind('<Return>', self.on_enter)
        
        self.listbox = tk.Listbox(self.root, font=("SF Meta", 12),
                                 fg='#ffffff', bg='#1e1e1e',
                                 relief=tk.FLAT, activestyle='none',
                                 selectbackground='#09c',
                                 selectforeground='#ffffff')
        self.listbox.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))
        self.listbox.bind('<<ListboxSelect>>', self.on_select)
        self.listbox.bind('<Double-Button-1>', self.on_double_click)
    
    def load_config(self):
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r") as f:
                self.config = json.load(f)
        else:
            self.config = DEFAULT_CONFIG.copy()
            save_config(self.config)
        self.keywords = list(self.config.get("mappings", {}).keys())
        self.show_suggestions("")
    
    def setup_hotkey(self):
        def toggle():
            self.shown = not self.shown
            if self.shown:
                self.root.deiconify()
                self.entry.focus_set()
                self.entry.select_range(0, tk.END)
            else:
                self.root.withdraw()
        
        keyboard.add_hotkey(SHOW_HOTKEY, toggle)
    
    def show_suggestions(self, text):
        matching = [k for k in self.keywords if text.lower() in k.lower()]
        matching = matching[:5]  # Show top 5
        
        self.listbox.delete(0, tk.END)
        for kw in matching:
            self.listbox.insert(tk.END, kw)
        
        if matching:
            self.listbox.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))
        else:
            self.listbox.pack_forget()
    
    def on_key_release(self, event):
        text = self.entry.get()
        self.show_suggestions(text)
    
    def on_enter(self, event):
        keyword = self.entry.get().strip()
        if keyword:
            self.execute_keyword(keyword)
            self.shown = False
            self.root.withdraw()
            self.entry.delete(0, tk.END)
    
    def on_select(self, event):
        selection = self.listbox.curselection()
        if selection:
            keyword = self.listbox.get(selection[0])
            self.entry.delete(0, tk.END)
            self.entry.insert(0, keyword)
            self.on_enter(None)
    
    def on_double_click(self, event):
        selection = self.listbox.curselection()
        if selection:
            keyword = self.listbox.get(selection[0])
            self.entry.delete(0, tk.END)
            self.entry.insert(0, keyword)
            self.on_enter(None)
    
    def execute_keyword(self, keyword):
        mappings = self.config.get("mappings", {})
        directories = self.config.get("directories", {})
        websites = self.config.get("websites", {})
        
        if keyword in mappings:
            val = mappings[keyword]
            if val.startswith("http://") or val.startswith("https://"):
                webbrowser.open(val)
            else:
                subprocess.Popen([val] if os.path.exists(val) else ["cmd", "/c", val])
        elif keyword in directories:
            path = directories[keyword]
            if os.path.exists(path):
                subprocess.Popen([path] if os.path.isfile(path) else ["explorer", "/select,", path])
            else:
                messagebox.showwarning("Not found", f"Directory not found: {path}")
        elif keyword in websites:
            webbrowser.open(websites[keyword])
        elif keyword == "calc":
            self.show_calc()
        elif keyword == "prettycode":
            self.show_prettycode()
        else:
            messagebox.showinfo("Not found", f"No mapping for: {keyword}")
    
    def show_calc(self):
        calc = tk.Toplevel(self.root)
        calc.title("Calculator")
        calc.geometry("300x400")
        calc.configure(bg='#252526')
        
        expr = tk.StringVar()
        entry = tk.Entry(calc, textvariable=expr, font=("SF Mono", 14), 
                        fg='white', bg='#252526', insertbackground='white')
        entry.pack(fill=tk.X, padx=10, pady=10)
        
        def evaluate():
            try:
                result = str(eval(expr.get()))
                messagebox.showinfo("Result", result, parent=calc)
            except:
                messagebox.showerror("Error", "Invalid", parent=calc)
        
        tk.Button(calc, text="=", command=evaluate, bg='#3c3c3c', fg='white',
                 font=("SF Mono", 12)).pack(pady=5)
    
    def show_prettycode(self):
        pw = tk.Toplevel(self.root)
        pw.title("PrettyCode")
        pw.geometry("800x600")
        pw.configure(bg='#1e1e1e')
        
        left = tk.Frame(pw, relief=tk.SUNKEN, bg='#1e1e1e')
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        tk.Label(left, text="Raw Code", fg='white', bg='#1e1e1e', font=("SF Mono", 10)).pack(anchor="w")
        raw = scrolledtext.ScrolledText(left, height=20, fg='white', bg='#252526', 
                                       insertbackground='white')
        raw.pack(fill=tk.BOTH, expand=True)
        
        right = tk.Frame(pw, relief=tk.SUNKEN, bg='#1e1e1e')
        right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        tk.Label(right, text="Prettified", fg='white', bg='#1e1e1e', font=("SF Mono", 10)).pack(anchor="w")
        pretty = scrolledtext.ScrolledText(right, height=20, fg='white', bg='#252526',
                                          insertbackground='white')
        pretty.pack(fill=tk.BOTH, expand=True)
        
        def prettify():
            code = raw.get("1.0", tk.END)
            lines = [line.rstrip() for line in code.strip().split("\n")]
            pretty.delete("1.0", tk.END)
            pretty.insert("1.0", "\n".join(lines))
        
        def copy_pretty():
            text = pretty.get("1.0", tk.END)
            if text:
                self.root.clipboard_clear()
                self.root.clipboard_append(text)
                messagebox.showinfo("Copied", "Code copied!")
        
        tk.Button(left, text="Prettify", command=prettify, bg='#3c3c3c', fg='white',
                 font=("SF Mono", 9)).pack(pady=5)
        tk.Button(right, text="Copy", command=copy_pretty, bg='#3c3c3c', fg='white',
                 font=("SF Mono", 9)).pack(pady=5)
        
        prettify()
    
    def run(self):
        self.root.mainloop()

def save_config(config):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)

if __name__ == "__main__":
    Launcher().run()