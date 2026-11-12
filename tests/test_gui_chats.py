"""Offline graphical checks; skip cleanly when Tcl/Tk cannot reach a display."""
import tkinter as tk
import unittest
from types import SimpleNamespace

from infobot.chats import Chats
from infobot.client_view import ClientMenu
from infobot.demo import Demo
from infobot.engine import Engine
from infobot.faq import load_faq
from infobot.runner import Runner, Delivery
from infobot.settings import ROOT, Settings
from infobot.store import Store
from test_bot import FakeAPI


class GraphicalChatsTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root=tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        self.root.geometry('1000x800')
        self.addCleanup(self.root.destroy)
        self.store=Store(':memory:')
        self.addCleanup(self.store.close)
        self.settings=Settings(operator_id=900)
        self.engine=Engine(self.store,self.settings,load_faq(ROOT/'faq.json'))
        with self.store.db:
            self.a=self.store.create_ticket(100,'Олена')
            self.b=self.store.create_ticket(200,'Іван')
            self.store.add_message(self.a,'client','Перше питання')
            self.store.add_message(self.b,'client','Друге питання')
        self.commands=[]
        self.chats=Chats(self.root,self.control)
        self.chats.pack(fill='both',expand=True)
        self.refresh()

    def control(self,action):
        self.commands.append(action)
        Runner.chat_control(SimpleNamespace(settings=self.settings),self.engine,self.store,
                            {**action,'operator_id':900})
        if action['ticket_id'] in self.chats.tabs:
            self.chats.tabs[action['ticket_id']]['awaiting']=False

    def refresh(self):
        self.chats.snapshot(self.store.chat_snapshot())
        self.root.update()

    def test_two_chats_reopen_draft_minimize_and_unread(self):
        self.chats.open(self.a)
        self.root.update()
        entry=self.chats.tabs[self.a]['entry']
        entry.insert('1.0','Чернетка А')
        self.root.update()
        self.chats.open(self.b)
        self.root.update()
        self.chats.open(self.a)
        self.root.update()
        self.assertEqual(len(self.chats.tabs),2)
        self.assertEqual(entry.get('1.0','end-1c'),'Чернетка А')
        self.assertEqual(self.root.focus_get(),entry)
        self.chats.show_list()
        self.root.update()
        with self.store.db:
            self.store.add_message(self.a,'client','Після згортання')
        self.refresh()
        self.assertEqual(self.chats.rows[self.a]['unread'],1)
        self.assertIn('1',self.chats.label.get())
        self.assertIsNotNone(self.store.ticket(self.a))
        self.chats.open(self.a)
        self.root.update()
        self.refresh()
        self.assertEqual(self.chats.rows[self.a]['unread'],0)
        self.assertEqual(entry.get('1.0','end-1c'),'Чернетка А')

    def test_send_switch_and_transport_confirmation(self):
        self.chats.open(self.a)
        self.root.update()
        self.chats.tabs[self.a]['entry'].insert('1.0','Відповідь А')
        self.root.update()
        self.chats.send(self.a)
        self.chats.send(self.a)
        self.chats.open(self.b)
        self.refresh()
        api=FakeAPI();delivery=Delivery(self.store,api,lambda *args:None)
        for _ in range(10):
            delivery.sent_at.clear();delivery.flush()
        self.refresh()
        self.assertEqual(sum(p.get('chat_id')==100 for _,p in api.calls),1)
        self.assertIn('Повідомлення надіслано клієнту.',self.chats.tabs[self.a]['status'].get())
        self.assertEqual(self.chats.tabs[self.a]['entry'].get('1.0','end-1c'),'')
        self.assertEqual(self.chats.active,self.b)

    def test_short_long_menu_resize_and_scroll_reset(self):
        self.chats.pack_forget()
        menu=ClientMenu(self.root);menu.pack(fill='both',expand=True)
        menu.show('Коротка відповідь.\n\nАбзац зліва.')
        self.root.update()
        self.assertGreater(menu.canvas.coords(menu.window)[1],100)
        self.assertEqual(str(menu.text.cget('justify')),'left')
        self.root.geometry('740x540');self.root.update()
        self.assertGreater(menu.canvas.coords(menu.window)[1],0)
        menu.show(('Довга відповідь. '*20+'\n')*60);self.root.update()
        self.assertEqual(menu.canvas.coords(menu.window)[1],0)
        menu.canvas.yview_moveto(1);self.root.update()
        self.assertGreater(menu.canvas.yview()[0],0)
        menu.show('Інша відповідь');self.root.update()
        self.assertEqual(menu.canvas.yview()[0],0)
        self.assertGreater(menu.canvas.coords(menu.window)[1],0)

    def test_invalid_reply_stays_in_editor(self):
        self.chats.open(self.a)
        self.root.update()
        entry = self.chats.tabs[self.a]['entry']
        text = '🙂' * 1001
        entry.insert('1.0', text)
        self.root.update()
        self.chats.send(self.a)
        self.assertEqual(entry.get('1.0', 'end-1c'), text)
        self.assertIn('2000', self.chats.tabs[self.a]['status'].get())
        self.assertFalse(any(c['action'] == 'send' for c in self.commands))

    def test_safe_demo_initialization_and_menu_navigation(self):
        self.chats.pack_forget()
        demo=Demo(self.root);demo.pack(fill='both',expand=True)
        self.addCleanup(demo.destroy)
        self.root.update()
        demo.click(1,'n:'+str(demo.engine.nav_state(1)['revision'])+':s:D01')
        self.root.update()
        self.assertIsNotNone(demo.specialist_chats)
        self.assertTrue(demo.client_menu.text.cget('text'))

    def test_demo_sources_screen_and_return_use_shared_menu(self):
        self.chats.pack_forget()
        demo = Demo(self.root)
        demo.pack(fill='both', expand=True)
        self.addCleanup(demo.destroy)
        self.root.update()
        for route in ('s:D01', 's:R00', 's:R50', 's:M02', 's:F00', 's:F02'):
            demo.click(1, f"n:{demo.engine.nav_state(1)['revision']}:{route}")
            self.root.update()
        def visible_buttons():
            return [button for row in demo.buttons[1].winfo_children() for button in row.winfo_children()]
        buttons = visible_buttons()
        self.assertEqual(sum(b.cget('text') == 'Джерела' for b in buttons), 1)
        before = demo.client_menu.text.cget('text')
        menu_id = demo.store.menu_message(1)['message_id']
        next(b for b in buttons if b.cget('text') == 'Джерела').invoke()
        self.root.update()
        self.assertTrue(demo.client_menu.text.cget('text').startswith('Документи до цієї відповіді'))
        self.assertEqual(demo.store.menu_message(1)['message_id'], menu_id)
        buttons = visible_buttons()
        self.assertIn('Порядок надання грантів № 739 · п. 4', [b.cget('text') for b in buttons])
        next(b for b in buttons if b.cget('text') == 'Назад до відповіді').invoke()
        self.root.update()
        self.assertEqual(demo.client_menu.text.cget('text'), before)
