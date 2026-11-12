"""Responsive local preview; Telegram itself controls message positioning."""
from tkinter import ttk
import tkinter as tk


class ClientMenu(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, background='white')
        scrollbar = ttk.Scrollbar(self, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        self.canvas.pack(fill='both', expand=True)
        self.block = ttk.Frame(self.canvas, padding=16)
        self.text = ttk.Label(self.block, justify='left', anchor='w')
        self.text.pack(fill='x')
        self.buttons = ttk.Frame(self.block)
        self.buttons.pack(fill='x', pady=(12, 0))
        self.window = self.canvas.create_window(0, 0, window=self.block, anchor='nw')
        self.canvas.bind('<Configure>', self.layout)
        self.block.bind('<Configure>', self.layout)
        self.canvas.bind('<MouseWheel>', self.wheel)
        self.block.bind('<MouseWheel>', self.wheel)
        self.text.bind('<MouseWheel>', self.wheel)
        self.canvas.bind('<Button-4>', lambda event: self.canvas.yview_scroll(-3, 'units'))
        self.canvas.bind('<Button-5>', lambda event: self.canvas.yview_scroll(3, 'units'))

    def wheel(self, event):
        self.canvas.yview_scroll(-1 if event.delta > 0 else 1, 'units')

    def layout(self, event=None):
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        block_width = min(width, 720)
        self.canvas.itemconfigure(self.window, width=block_width)
        self.text.configure(wraplength=max(40, block_width - 32))
        requested = self.block.winfo_reqheight()
        top = max(0, (height - requested) // 2)
        self.canvas.coords(self.window, max(0, (width - block_width) // 2), top)
        self.canvas.configure(scrollregion=(0, 0, width, max(height, requested)))

    def show(self, text):
        self.text.configure(text=text)
        self.after_idle(self.layout)
        self.canvas.yview_moveto(0)
