from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock
from urllib.error import HTTPError
import xml.etree.ElementTree as ET

import media_readers as readers
import server


def xml(values, tag='response'):
    root=ET.Element(tag)
    for key,value in values.items():ET.SubElement(root,key).text=str(value)
    return root


class RokuTests(unittest.TestCase):
    def test_partial_access_keeps_real_power_app_and_uptime(self):
        info=ET.parse(Path(__file__).parent / 'fixtures' / 'roku' / 'ultra-device-info.xml').getroot()
        active=xml({'app':'Roku Dynamic Menu'}, 'active-app')
        blocked=HTTPError('local',403,'restricted',{},None)
        self.addCleanup(blocked.close)
        with patch.object(readers,'read_xml',side_effect=[info,active,blocked]) as query:
            result=readers.roku('10.23.1.30')
        self.assertEqual(result['status'],'online')
        self.assertEqual(result['metrics']['power_mode']['value'],'Standby')
        self.assertEqual(result['metrics']['uptime_s']['value'],1234)
        self.assertEqual(result['metrics']['active_app']['value'],'Roku Dynamic Menu')
        self.assertNotIn('playback_state',result['metrics'])
        self.assertNotIn('advertising-id',str(result))
        self.assertIn('restricts',result['telemetry_note'])
        self.assertEqual(result['reported_name'],'Living room Roku')
        self.assertTrue(all('/query/' in call.args[1] for call in query.call_args_list))
        check = result['firmware_check']
        self.assertEqual(check['installed_version'], '16.0.4 (build 853)')
        self.assertEqual((check['status'], check['available_version']), ('unknown', None))


class UPnPTests(unittest.TestCase):
    def test_control_requests_allow_read_queries_only(self):
        with patch.object(readers,'build_opener') as opener:
            for action in ['SetVolume','Play','Stop','SetMute','AddURIToQueue','Reboot']:
                with self.subTest(action=action), self.assertRaises(ValueError):
                    readers.read_xml('10.23.1.27','http://10.23.1.27:1400/control',action,'urn:schemas-upnp-org:service:RenderingControl:1')
            opener.assert_not_called()

    def test_other_hosts_credentials_and_redirect_targets_are_rejected(self):
        for url in ['https://10.23.1.27/x','http://8.8.8.8/x','http://10.23.1.99/x','http://user:pass@10.23.1.27/x','http://10.23.1.27/x#bad']:
            with self.subTest(url=url), self.assertRaises(ValueError):readers.safe_url('10.23.1.27',url)

    def test_get_volume_uses_soap_query_envelope(self):
        response=io.BytesIO(b'<response><CurrentVolume>20</CurrentVolume></response>')
        opener=Mock();opener.open.return_value=response
        with patch.object(readers,'build_opener',return_value=opener):
            result=readers.read_xml('10.23.1.27','http://10.23.1.27:1400/control','GetVolume','urn:schemas-upnp-org:service:RenderingControl:1',{'InstanceID':0,'Channel':'Master'})
        request=opener.open.call_args.args[0]
        self.assertIn(b'GetVolume',request.data)
        self.assertNotIn(b'SetVolume',request.data)
        self.assertEqual(readers.leaf_values(result)['CurrentVolume'],'20')

    def test_component_level_is_not_labeled_room_volume(self):
        info={'model':'Sonos One SL','services':{'RenderingControl':('urn:schemas-upnp-org:service:RenderingControl:1','http://10.23.1.42:1400/control')},'udn':'uuid:RINCON_AABBCCDDEEFF01400','firmware':None,'name':'component'}
        def query(ip,url,action,*args):return xml({'CurrentVolume':20}) if action=='GetVolume' else xml({'CurrentMute':0})
        with patch.object(readers,'descriptor',return_value=info),patch.object(readers,'read_xml',side_effect=query):result=readers.upnp('10.23.1.42','http://10.23.1.42:1400/device.xml',sonos=True)
        self.assertIn('component_volume_pct',result['metrics'])
        self.assertNotIn('volume_pct',result['metrics'])
        self.assertNotIn('playback_state',result['metrics'])
        self.assertIn('coordinator',result['telemetry_note'])

    def test_unknown_track_duration_is_omitted(self):
        self.assertIsNone(readers.hhmmss('NOT_IMPLEMENTED'))
        self.assertEqual(readers.hhmmss('01:02:03'),3723)
        self.assertIsNone(readers.hhmmss('00:99:00'))
        self.assertIsNone(readers.numeric(float('nan')))


class CastTests(unittest.TestCase):
    def test_idle_receiver_does_not_claim_device_power_off(self):
        client=SimpleNamespace(status=SimpleNamespace(volume_level=.15,volume_muted=False,display_name=None),
                               is_idle=True,wait=Mock(),disconnect=Mock())
        factory=Mock(return_value=client)
        package=SimpleNamespace(Chromecast=factory)
        models=SimpleNamespace(CastInfo=lambda *args:args,HostServiceInfo=lambda *args:args)
        with patch.dict('sys.modules',{'pychromecast':package,'pychromecast.models':models}):result=readers.cast('10.23.1.51',{'name':'Speaker'})
        self.assertEqual(result['metrics']['volume_pct']['value'],15)
        self.assertEqual(result['metrics']['receiver_state']['value'],'Idle')
        self.assertNotIn('power_mode',result['metrics'])
        self.assertNotIn('playback_state',result['metrics'])
        self.assertIn('does not prove',result['telemetry_note'])
        client.disconnect.assert_called_once()

    def test_active_cast_reads_metadata_without_playback_commands(self):
        listeners=[]
        status=SimpleNamespace(player_state='PLAYING',title='A song',artist='An artist',album_name='An album',current_time=12,duration=180,content_id='https://example.invalid/private-token')
        media=SimpleNamespace(status=status,register_status_listener=lambda listener:listeners.append(listener),update_status=lambda:listeners[0].new_media_status(status))
        client=SimpleNamespace(status=SimpleNamespace(volume_level=.4,volume_muted=True,display_name='Music app'),is_idle=False,wait=Mock(),disconnect=Mock(),media_controller=media)
        package=SimpleNamespace(Chromecast=lambda *args,**kwargs:client)
        models=SimpleNamespace(CastInfo=lambda *args:args,HostServiceInfo=lambda *args:args)
        with patch.dict('sys.modules',{'pychromecast':package,'pychromecast.models':models}):result=readers.cast('10.23.1.51',{'name':'Speaker'})
        self.assertEqual(result['metrics']['playback_state']['value'],'Playing')
        self.assertEqual(result['metrics']['media_title']['value'],'A song')
        self.assertEqual(result['metrics']['media_position_s']['value'],12)
        self.assertEqual(result['metrics']['muted']['value'],1)
        self.assertNotIn('private-token',str(result))

    def test_shelly_reader_cannot_be_overridden_by_custom_media_name(self):
        self.assertIsNone(readers.select_reader({'name':'Sonos heater','protocol':'shelly-gen1'}))
        self.assertEqual(readers.select_reader({'name':'Custom speaker','protocol':'google-cast'}),'google-cast')
        self.assertIsNone(readers.select_reader({'name':'Garage Door Opener','hostname':'myq.lan'}))


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=server.Store(Path(self.tmp.name)/'media.sqlite3')
        self.ip='10.23.1.80';self.store.add_device(self.ip)

    def tearDown(self):self.store.db.close();self.tmp.cleanup()

    def test_text_changes_are_exported_but_numeric_charts_stay_numeric(self):
        for state in ['Playing','Playing','Paused']:
            self.store.record(self.ip,{'status':'online','reachable':True,'checked_at':server.now(),'last_telemetry':server.now(),
                'metrics':{'volume_pct':{'value':20,'unit':'%','label':'Volume'},'playback_state':{'value':state,'unit':'','label':'Playback'}}})
        rows=self.store.history(ip=self.ip)
        self.assertEqual(len([r for r in rows if r['metric']=='playback_state']),2)
        self.assertEqual(len([r for r in rows if r['metric']=='volume_pct']),3)
        chart=self.store.history(ip=self.ip,chart=True)
        self.assertTrue(all(isinstance(r['value'],(float,int)) for r in chart))
        self.assertEqual(self.store.devices()[-1]['metrics']['playback_state']['value'],'Paused')

    def test_csv_formula_text_is_escaped_but_negative_numbers_are_not(self):
        self.assertEqual(server.csv_value('=HYPERLINK("bad")'),'\'=HYPERLINK("bad")')
        self.assertEqual(server.csv_value(-72),-72)
        self.assertEqual(server.csv_value('Playing'),'Playing')

    def test_poorer_name_discovery_does_not_replace_friendly_name(self):
        self.store.apply_names([{'ip':self.ip,'name':'Living room','source':'mDNS friendly name'}])
        self.store.apply_names([{'ip':self.ip,'name':'speaker-123','source':'Local DNS'}])
        d=next(d for d in self.store.devices() if d['ip']==self.ip)
        self.assertEqual(d['name'],'Living room')
        self.store.apply_names([{'ip':self.ip,'name':'Bedroom','source':'mDNS friendly name'}])
        self.assertEqual(next(d for d in self.store.devices() if d['ip']==self.ip)['name'],'Bedroom')


if __name__=='__main__':unittest.main()
