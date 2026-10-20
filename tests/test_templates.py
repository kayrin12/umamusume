from copy import deepcopy
from pathlib import Path
import tempfile
import time
import unittest
from PIL import Image,ImageDraw,ImageFont
from umacoach.templates import TemplateBank,TemplateError,crop
from umacoach.scoring import Weights,score,recovery,validate_weights
from umacoach.training import ranking,validate_preview
from umacoach.session import Session


def number_image(text,size=32):
    # Synthetic test font only, never packaged as a client template.
    font=ImageFont.load_default(size=size)
    pic=Image.new('RGB',(max(50,round(len(text)*size*.75)+20),size+20),'black')
    ImageDraw.Draw(pic).text((8,4),text,font=font,fill='white')
    return pic


def train_digits(bank):
    for char in '0123456789+':bank.add(number_image(char),'gain_speed',[0,0,1,1],char,sample_type='glyph')


def icon(color,mark):
    pic=Image.new('RGB',(40,40),color);d=ImageDraw.Draw(pic)
    for i in range(mark+1):d.rectangle((i*5,4,i*5+2,30),fill='white')
    return pic


class Templates(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.bank=TemplateBank(Path(self.tmp.name)/'templates')

    def test_glyphs_from_self_samples_recognize_digits_and_scale(self):
        train_digits(self.bank)
        pic=number_image('+120')
        self.bank.add(pic,'gain_speed',[0,0,1,1],'+120')
        self.assertEqual(self.bank.recognize(pic)['values']['gain_speed'],120)
        bigger=pic.resize((pic.width*2,pic.height*2))
        self.assertEqual(self.bank.recognize(bigger)['values']['gain_speed'],120)
        self.assertEqual(TemplateBank(self.bank.root).recognize(pic)['values']['gain_speed'],120)

    def test_missing_digits_are_unknown_not_invented_zero(self):
        self.bank.add(number_image('+120'),'gain_speed',[0,0,1,1],'+120')
        self.assertIsNone(self.bank.recognize(number_image('+120'))['values']['gain_speed'])
        self.assertIn('9',self.bank.status()['missing_digits'])

    def test_blank_gain_requires_explicit_self_sample_and_never_masks_new_digits(self):
        train_digits(self.bank);blank=Image.new('RGB',(130,54),'black')
        self.bank.add(blank,'gain_stamina',[0,0,1,1],'',sample_type='region')
        self.assertIsNone(self.bank.recognize(blank)['values']['gain_stamina'])
        self.bank.add(blank,'gain_stamina',[0,0,1,1],'0',sample_type='zero')
        self.assertEqual(self.bank.recognize(blank)['values']['gain_stamina'],0)
        positive=blank.copy();ImageDraw.Draw(positive).text((8,4),'+12',font=ImageFont.load_default(size=32),fill='white')
        self.assertEqual(self.bank.recognize(positive)['values']['gain_stamina'],12)
        self.bank.add(number_image('0'),'gain_speed',[0,0,1,1],'0')
        self.assertEqual(self.bank.recognize(number_image('0'))['values']['gain_speed'],0)
        with self.assertRaises(TemplateError):self.bank.add(blank,'failure',[0,0,1,1],'0',sample_type='zero')

    def test_unchanged_fields_do_not_repeat_matching(self):
        train_digits(self.bank);pic=number_image('+120');self.bank.add(pic,'gain_speed',[0,0,1,1],'+120')
        self.bank.recognize(pic)
        result=self.bank.recognize(pic)
        self.assertEqual(result['metrics']['processed_fields'],0)
        self.assertEqual(self.bank.recognize(number_image('+125'))['values']['gain_speed'],125)

    def test_manual_override_is_bound_to_exact_field_and_rejects_stale_frame(self):
        train_digits(self.bank);pic=number_image('+120');self.bank.add(pic,'gain_speed',[0,0,1,1],'+120')
        before=self.bank.recognize(pic)
        self.bank.override(pic,'gain_speed',121,before['field_hashes']['gain_speed'])
        self.assertEqual(self.bank.recognize(pic)['values']['gain_speed'],121)
        self.assertEqual(self.bank.recognize(number_image('+125'))['values']['gain_speed'],125)
        with self.assertRaises(TemplateError):self.bank.override(number_image('+125'),'gain_speed',9,before['field_hashes']['gain_speed'])

    def test_icons_require_all_selected_training_classes(self):
        names=['速度','耐力','力量','毅力','智力'];colors=['red','blue','green','orange','purple']
        self.bank.add(icon(colors[0],0),'selected',[0,0,1,1],names[0],sample_type='icon')
        self.assertIsNone(self.bank.recognize(icon(colors[0],0))['values']['selected'])
        for i in range(1,5):self.bank.add(icon(colors[i],i),'selected',[0,0,1,1],names[i],sample_type='icon')
        self.assertEqual(self.bank.recognize(icon(colors[2],2))['values']['selected'],'力量')

    def test_support_slot_templates_share_bank_and_unknown_stays_unknown(self):
        yes,no=icon('yellow',1),icon('black',4)
        self.bank.add(yes,'card0_friendship',[0,0,1,1],'yes',sample_type='icon')
        self.assertIsNone(self.bank.recognize(yes)['values']['card0_friendship'])
        self.bank.add(no,'card1_friendship',[0,0,1,1],'no',sample_type='icon')
        self.assertEqual(self.bank.recognize(yes)['values']['card1_friendship'],'yes')

    def test_energy_pixel_ratio_and_guide_cells_are_calibrated_from_user(self):
        pic=Image.new('RGB',(100,20),'black');ImageDraw.Draw(pic).rectangle((0,0,59,19),fill='green')
        self.bank.add(pic,'energy',[0,0,1,1],'60',sample_type='meter')
        live=Image.new('RGB',(100,20),'black');ImageDraw.Draw(live).rectangle((0,0,34,19),fill='green')
        self.assertEqual(self.bank.recognize(live)['values']['energy'],35)
        bar=Image.new('RGB',(80,16),'black');ImageDraw.Draw(bar).rectangle((0,0,29,15),fill='blue')
        self.bank.add(bar,'guide_blue',[0,0,1,1],'3',sample_type='meter')
        live=Image.new('RGB',(80,16),'black');ImageDraw.Draw(live).rectangle((0,0,49,15),fill='blue')
        self.assertEqual(self.bank.recognize(live)['values']['guide_blue'],5)

    def test_full_only_meter_without_empty_color_cannot_guess(self):
        pic=Image.new('RGB',(100,20),'green')
        self.bank.add(pic,'energy',[0,0,1,1],'100',sample_type='meter')
        self.assertIsNone(self.bank.recognize(pic)['values']['energy'])

    def test_unfamiliar_meter_color_is_unknown_until_sampled(self):
        pic=Image.new('RGB',(100,20),'black');ImageDraw.Draw(pic).rectangle((0,0,59,19),fill='green')
        self.bank.add(pic,'energy',[0,0,1,1],'60',sample_type='meter')
        red=Image.new('RGB',(100,20),'black');ImageDraw.Draw(red).rectangle((0,0,19,19),fill='red')
        self.assertIsNone(self.bank.recognize(red)['values']['energy'])
        self.bank.add(red,'energy',[0,0,1,1],'20',sample_type='meter')
        self.assertEqual(self.bank.recognize(red)['values']['energy'],20)
        self.assertEqual(self.bank.recognize(pic)['values']['energy'],60)

    def test_wrong_sample_count_and_unsafe_paths_are_rejected(self):
        with self.assertRaises(TemplateError):self.bank.add(number_image('12'),'gain_speed',[0,0,1,1],'123')
        with self.assertRaises(TemplateError):self.bank.remove('../index.json')
        with self.assertRaises(TemplateError):crop(number_image('1'),[0,0,float('nan'),1])
        self.bank.add(number_image('1'),'gain_speed',[0,0,1,1],'1',sample_type='glyph')
        file=self.bank.data['samples'][0]['file'];self.bank.remove(file)
        self.assertFalse((self.bank.root/file).exists());self.assertIn('1',self.bank.status()['missing_digits'])


class ConfigurableScoring(unittest.TestCase):
    def setUp(self):self.config=Weights().config;self.state=dict(attributes=dict(speed=99),guide_gauges={},mentor='未定')
    def preview(self,kind='速度',gains=None,**kw):return validate_preview(dict(kind=kind,gains=gains or [10,0,0,0,0,10],failure=0,**kw))

    def test_caps_and_stage_targets_change_effective_gains_and_order(self):
        p=self.preview();base=score(p,self.state,40,self.config)
        self.config['attribute_caps'][0]=100
        capped=score(p,self.state,40,self.config)
        self.assertEqual(capped['effective_gains'][0],1);self.assertLess(capped['score'],base['score'])
        self.config['phases']['classic']['targets'][0]=1000
        self.assertGreater(score(p,self.state,40,self.config)['score'],capped['score'])

    def test_failure_loss_and_hint_bonuses_are_explicit(self):
        p=self.preview(hints=2);good=score(p,self.state,40,self.config)
        p['failure']=50;bad=score(p,self.state,40,self.config)
        self.assertLess(bad['score'],good['score']);self.assertIn('失败概率',bad['explanation'])

    def test_summer_and_fragment_rules_change_weights(self):
        p=self.preview(color='pink',friends=1);self.state['guide_gauges']['pink']=7
        summer=score(p,self.state,36,self.config);regular=score(p,self.state,40,self.config)
        self.assertGreater(summer['score'],regular['score']);self.assertIn('夏合宿',summer['explanation'])

    def test_all_five_seen_outputs_concrete_turn_action(self):
        cache={kind:dict(at=0,preview=self.preview(kind)) for kind in ['速度','耐力','力量','毅力','智力']}
        result=ranking(cache,self.state,40,now=0)
        self.assertIn('本回合选',result['recommendation']);self.assertEqual(result['missing'],[])

    def test_rest_outing_and_infirmary_have_deterministic_thresholds(self):
        for live,action in [(dict(energy_percent=5),'rest'),(dict(mood='絕不調'),'outing'),(dict(ailment=True),'infirmary')]:
            candidates=recovery(self.state,live,self.config)
            self.assertEqual(candidates[0]['action'],action)
        self.assertEqual(recovery(self.state,{},self.config),[])

    def test_json_validation_and_persistent_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            weights=Weights(Path(tmp)/'training_weights.json');weights.save(self.config)
            self.assertEqual(Weights(weights.path).config,self.config)
        for change in [dict(failure_expected_loss=float('nan')),dict(phases=[]),dict(version=True),dict(rest_below_percent=101)]:
            with self.assertRaises(ValueError):validate_weights(self.config|change)


class FastPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.s=Session(Path(self.tmp.name));self.s.tracker.new('貴婦人')
        self.s.local_coach.settings.update(enabled=True,backend='templates')
        # Generates artificial shapes/font samples, not client assets.
        import importlib.util
        spec=importlib.util.spec_from_file_location('template_benchmark_fixture',Path(__file__).resolve().parents[1]/'scripts/benchmark_templates.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.image,self.regions=module.fixture(self.s.local_coach.fast.bank)

    def read(self):
        with self.s.lock:self.s.local_coach.submit(self.image,True)
        deadline=time.monotonic()+3
        while self.s.local_coach.fast.busy and time.monotonic()<deadline:time.sleep(.005)
        self.assertFalse(self.s.local_coach.fast.busy)

    def test_date_digits_cards_and_ranking_without_any_ocr_or_model(self):
        from unittest.mock import patch
        with patch.object(self.s.local_coach.ocr,'recognize') as ocr,patch('umacoach.session.codex_backend.analyze_codex') as model:
            self.read();self.read()
        snap=self.s.snapshot()['local'];self.assertEqual(snap['date']['calendar_index'],32)
        self.assertEqual(snap['fast']['live']['counts'],dict(participants=2,friends=1,hints=1,unbonded=1))
        self.assertEqual(snap['training']['rows'][0]['kind'],'速度')
        self.assertEqual(snap['training']['rows'][0]['gains'],[20,0,8,0,0,35])
        self.assertEqual(self.s.tracker.active.local['training']['速度']['source'],'self_templates')
        ocr.assert_not_called();model.assert_not_called()

    def test_unchanged_frame_does_not_resave_or_reclassify(self):
        self.read();self.read();revision=self.s.tracker.active.state_revision
        self.read()
        self.assertEqual(self.s.tracker.active.state_revision,revision)
        self.assertEqual(self.s.local_coach.fast.live['metrics']['processed_fields'],0)

    def test_training_channel_still_works_while_model_is_busy(self):
        self.s.busy=True
        self.read();self.read()
        self.assertTrue(self.s.busy)
        self.assertEqual(self.s.snapshot()['local']['training']['rows'][0]['kind'],'速度')

    def test_old_fast_response_cannot_enter_new_run(self):
        from unittest.mock import patch
        import threading
        bank=self.s.local_coach.fast.bank;original=bank.recognize
        entered,release=threading.Event(),threading.Event()
        def delayed(*args):entered.set();release.wait(2);return original(*args)
        with patch.object(bank,'recognize',side_effect=delayed):
            with self.s.lock:self.s.local_coach.submit(self.image,True)
            self.assertTrue(entered.wait(1));self.s.boundary('new');release.set()
            deadline=time.monotonic()+3
            while self.s.local_coach.fast.busy and time.monotonic()<deadline:time.sleep(.005)
        self.assertEqual(self.s.tracker.active.local,{})
        self.assertIsNone(self.s.snapshot()['local']['training'])


if __name__=='__main__':unittest.main()
