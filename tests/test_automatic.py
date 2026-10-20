"""Synthetic layout evidence, never a claim of actual client OCR accuracy."""
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from PIL import Image,ImageDraw,ImageFont
from umacoach.automatic import parse_screen,stat_rows
from umacoach.session import Session


def fixture(kind='速度',date='古典年5月前半',values=None):
    values=values or [20,0,8,0,0,35]
    words=[dict(text=date,x=20,y=20,w=250,h=25),dict(text='還有3回合',x=20,y=54,w=180,h=25),
           dict(text=kind+'訓練',x=40,y=350,w=180,h=25),dict(text='失敗率0%',x=40,y=850,w=180,h=25)]
    words += [dict(text=t,x=20+124*i,y=600,w=48,h=20) for i,t in enumerate(['速度','耐力','力量','毅力','智力','技能Pt'])]
    words += [dict(text='+'+str(n),x=20+124*i,y=560,w=55,h=25) for i,n in enumerate(values) if n]
    image=Image.new('RGB',(780,1000),'black');d=ImageDraw.Draw(image)
    font=ImageFont.load_default(size=25)
    # The synthetic font has no Chinese glyphs; make tab changes visible too.
    colors={'速度':'red','耐力':'blue','力量':'green','毅力':'orange','智力':'purple'}
    d.rectangle((40,350,220,375),fill=colors[kind])
    for w in words:d.text((w['x'],w['y']),w['text'],font=font,fill='white')
    if '後半' in date:d.rectangle((255,22,264,38),fill='cyan')
    return image,words


def ocr(image,words,regions):
    items=[]
    for key,r in regions.items():
        x,y,x2,y2=r[0]*image.width,r[1]*image.height,r[2]*image.width,r[3]*image.height
        subset=[dict(text=w['text'],x=w['x']-x,y=w['y']-y,w=w['w'],h=w['h']) for w in words if x<=w['x']+w['w']/2<=x2 and y<=w['y']+w['h']/2<=y2]
        items.append(dict(key=key,text=' '.join(w['text'] for w in subset),words=subset,width=x2-x,height=y2-y))
    return dict(items=items,elapsed_ms=25,language='zh-Hant')


class AutomaticParsing(unittest.TestCase):
    def read(self,image,words):return parse_screen(image,ocr(image,words,{'screen':[0,0,1,1]}),{'screen':[0,0,1,1]})

    def test_layout_and_zero_cells_are_found_without_samples_or_user_rectangles(self):
        image,words=fixture();p,layout=self.read(image,words)
        self.assertEqual(p['gains'],[20,0,8,0,0,35]);self.assertEqual(p['kind'],'速度');self.assertEqual(p['date']['calendar_index'],32)
        self.assertEqual(set(layout),{'gains','selected','failure','header','guard'})

    def test_unrecognised_digit_is_not_converted_into_blank_zero(self):
        image,words=fixture();ImageDraw.Draw(image).text((144,560),'9',font=ImageFont.load_default(size=25),fill='white')
        p,_=self.read(image,words)
        self.assertIsNone(p['gains'][1])

    def test_cropped_ocr_coordinates_map_back_to_the_client(self):
        image,words=fixture();_,layout=self.read(image,words);regions={k:r for k,r in layout.items() if k!='guard'}
        p,_=parse_screen(image,ocr(image,words,regions),regions)
        self.assertEqual(p['gains'],[20,0,8,0,0,35]);self.assertEqual(p['kind'],'速度')

    def test_ocr_split_plus_and_digits_of_different_heights_are_merged(self):
        image,words=fixture();word=next(w for w in words if w['text']=='+20');words.remove(word)
        words += [dict(text='+',x=20,y=568,w=12,h=12),dict(text='20',x=33,y=560,w=30,h=25)]
        p,_=self.read(image,words);self.assertEqual(p['gains'],[20,0,8,0,0,35])

    def test_absent_or_duplicate_columns_do_not_generate_a_training_choice(self):
        image,words=fixture();words=[w for w in words if w['text']!='技能Pt']
        p,layout=self.read(image,words);self.assertIsNone(p['gains']);self.assertEqual(layout,{})
        image,words=fixture();words += [dict(w,y=w['y']-200) for w in words if w['y'] in (600,560)]
        p,_=self.read(image,words);self.assertIsNone(p['gains'])

    def test_five_training_labels_are_not_assumed_to_indicate_selection(self):
        image,words=fixture();words=[w for w in words if w['y']!=350]
        words += [dict(text=t,x=30+145*i,y=920,w=48,h=20) for i,t in enumerate(['速度','耐力','力量','毅力','智力'])]
        p,_=self.read(image,words);self.assertIsNone(p['kind'])

    def test_unique_active_tab_can_be_located_from_its_highlight(self):
        image,words=fixture();words=[w for w in words if w['y']!=350]
        words += [dict(text=t,x=30+145*i,y=920,w=48,h=20) for i,t in enumerate(['速度','耐力','力量','毅力','智力'])]
        ImageDraw.Draw(image).rectangle((300,885,360,955),fill='yellow')
        p,_=self.read(image,words);self.assertEqual(p['kind'],'力量')

    def test_current_fans_are_separate_from_a_goal_requirement(self):
        image,words=fixture();words += [dict(text='粉絲數1,000人',x=30,y=100,w=240,h=25),dict(text='目標粉絲數至少6000人',x=30,y=150,w=300,h=25)]
        p,_=self.read(image,words);self.assertEqual(p['fans'],1000);self.assertEqual(p['goal_required'],6000)

    def test_unique_header_character_is_detected_without_selecting_a_profile(self):
        image,words=fixture();words.append(dict(text='貴婦人',x=350,y=100,w=120,h=25))
        p,_=self.read(image,words);self.assertEqual(p['character'],'貴婦人')
        words.append(dict(text='黃金巨匠',x=350,y=140,w=120,h=25))
        p,_=self.read(image,words);self.assertIsNone(p['character'])


class AutomaticIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.s=Session(Path(self.tmp.name));self.s.local_coach.settings.update(enabled=True,backend='auto')
        self.channel=self.s.local_coach.automatic;self.current=fixture();self.calls=[]
        def recognize(image,regions):self.calls.append(set(regions));return ocr(image,self.current[1],regions)
        self.mock=patch.object(self.s.local_coach.ocr,'recognize',side_effect=recognize).start();self.addCleanup(patch.stopall)

    def read(self):
        image=self.current[0]
        with self.s.lock:self.s.set_frame(image);self.s.local_coach.submit(image)
        end=time.monotonic()+3
        while self.channel.busy and time.monotonic()<end:time.sleep(.005)
        self.assertFalse(self.channel.busy)

    def stable(self):self.read();self.read()

    def test_one_start_auto_attaches_and_compares_all_five_without_configuration(self):
        self.channel.allow_attach=True
        self.stable();self.assertTrue(self.s.tracker.active)
        self.assertEqual(self.s.snapshot()['local']['training']['rows'][0]['kind'],'速度')
        for kind in ['耐力','力量','毅力','智力']:
            self.current=fixture(kind);self.channel.last_at=0;self.stable()
        result=self.s.snapshot()['local']['training'];self.assertEqual(len(result['rows']),5);self.assertIn('本回合选',result['recommendation'])
        self.assertFalse(self.s.local_coach.fast.bank.data['samples'])
        self.assertTrue(self.channel.path.exists())

    def test_unchanged_panel_does_not_reocr_or_resave_while_character_animates(self):
        self.channel.allow_attach=True;self.stable();count=len(self.calls);revision=self.s.tracker.active.state_revision
        ImageDraw.Draw(self.current[0]).rectangle((300,250,450,300),fill='blue')
        self.read();self.assertEqual(len(self.calls),count);self.assertEqual(self.s.tracker.active.state_revision,revision)

    def test_only_changed_regions_are_read_after_location(self):
        self.channel.allow_attach=True;self.stable()
        self.current=fixture('耐力');self.channel.last_at=0;self.stable()
        self.current=fixture('力量');self.channel.last_at=0;self.stable()
        self.assertEqual(self.calls[-1],{'selected'})

    def test_date_rollback_hides_advice_and_requires_run_reconciliation(self):
        self.channel.allow_attach=True;self.stable()
        self.current=fixture(date='初級年6月前半');self.channel.last_at=0;self.stable()
        self.assertTrue(self.s.tracker.pending);self.assertIsNone(self.s.snapshot()['local']['training'])

    def test_model_busy_does_not_block_automatic_local_reading(self):
        self.s.busy=True;self.channel.allow_attach=True;self.stable()
        self.assertEqual(self.s.snapshot()['local']['training']['rows'][0]['kind'],'速度')

    def test_next_half_clears_previous_previews_automatically(self):
        self.channel.allow_attach=True;self.stable()
        self.current=fixture('耐力','古典年5月後半');self.channel.last_at=0;self.stable()
        self.assertEqual(set(self.s.tracker.active.local['training']),{'耐力'})
        self.assertEqual(self.s.tracker.active.last_turn,33)

    def test_user_fixed_character_is_not_overwritten_by_header_ocr(self):
        self.s.tracker.new('黃金巨匠');self.s.tracker.active.character_locked=True
        self.current[1].append(dict(text='貴婦人',x=350,y=100,w=120,h=25));self.stable()
        self.assertEqual(self.s.tracker.active.character,'黃金巨匠')

    def test_incomplete_ocr_retries_without_requiring_manual_sampling(self):
        self.channel.allow_attach=True
        ImageDraw.Draw(self.current[0]).text((144,560),'9',font=ImageFont.load_default(size=25),fill='white')
        self.stable()
        self.assertEqual(self.s.tracker.active.local.get('training',{}),{})
        self.assertIn('自动重试',self.s.local_coach.message)
        before=len(self.calls);self.channel.audit=time.monotonic()-21;self.channel.last_at=0;self.read()
        self.assertGreater(len(self.calls),before)

    def test_old_ocr_result_cannot_learn_or_write_into_a_new_run(self):
        entered,release=threading.Event(),threading.Event();image,words=self.current
        def delayed(image,regions):entered.set();release.wait(2);return ocr(image,words,regions)
        self.channel.allow_attach=True
        with patch.object(self.s.local_coach.ocr,'recognize',side_effect=delayed):
            with self.s.lock:self.s.local_coach.submit(image)
            self.assertTrue(entered.wait(1));self.s.boundary('new');release.set()
            end=time.monotonic()+3
            while self.channel.busy and time.monotonic()<end:time.sleep(.005)
        self.assertEqual(self.s.tracker.active.local,{})
        self.assertFalse(self.channel.path.exists())


if __name__=='__main__':unittest.main()
