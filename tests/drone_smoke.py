"""Run against python3 -m http.server 8000; requires Python Playwright + Chromium.
Artifacts and synthesized fixtures are temporary; no microphone permission is used.
"""
import array
import math
import os
from pathlib import Path
import tempfile
import wave
from playwright.sync_api import sync_playwright

URL = os.environ.get('RYUON_TEST_URL', 'http://127.0.0.1:8000/ryuon_v06a_drone_test.html')

def fixture(path, count=4410, silent=False):
    with wave.open(str(path), 'wb') as w:
        w.setparams((1, 2, 44100, 0, 'NONE', 'not compressed'))
        # A short decaying drop, changing frequency across its duration.
        samples = array.array('h', (0 if silent else 12000 if count==1 else int(20000 * math.exp(-3*i/max(1,count)) * math.sin(2*math.pi*(400*i/44100+1200*i*i/(2*44100*max(1,count))))) for i in range(count)))
        w.writeframes(samples.tobytes())

def export(page, root, engine, seconds, silent=False):
    page.locator('#exportLength').select_option(str(seconds))
    with page.expect_download(timeout=120000) as dl:
        page.locator('#export').click()
    path = root / f'{engine}-{seconds}.wav'
    dl.value.save_as(path)
    with wave.open(str(path), 'rb') as w:
        assert (w.getnchannels(), w.getframerate(), w.getnframes()) == (2, 44100, seconds*44100)
        samples = array.array('h', w.readframes(w.getnframes()))
    peak = max(map(abs, samples))
    assert peak < 32767, peak
    assert peak == 0 if silent else peak > 10, peak
    if not silent and engine == 'freeze':
        # Both output channels must remain active at the end and across loop seams.
        for c in [0, 1]:
            for second in range(1, seconds):
                start = second*44100*2+c
                assert max(map(abs, samples[start:start+4410:2])) > 10
        # Start/end envelopes must reach silence; no full-scale jump at the seam.
        assert abs(samples[0]) < 10 and abs(samples[-1]) < 30
        seam = int(page.evaluate('spectralCache.duration / Math.pow(2,val("pitch")/12)')*44100)*2
        if seam+4 < len(samples):
            assert abs(samples[seam]-samples[seam-2]) < 5000
    print(f'PASS {engine} {seconds}s export, stereo peak={peak}')

with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
    root = Path(tmp)
    fixture(root/'drop.wav')
    fixture(root/'tiny.wav', 1)
    fixture(root/'silence.wav', 4410, True)
    browser = p.chromium.launch(executable_path='/usr/bin/chromium', headless=True, args=['--no-sandbox'])
    page = browser.new_page(viewport={'width':390,'height':844}, accept_downloads=True)
    errors=[]
    page.on('pageerror', lambda e: errors.append(str(e)))
    assert page.goto(URL).status == 200
    assert not page.locator('#spectralFreeze').is_visible()
    page.locator('#file').set_input_files(root/'drop.wav')
    page.wait_for_function('!!buffer')
    # Selection, trim, all existing edits and undo.
    page.evaluate('selection={a:.2,b:.8};drawWave()')
    page.locator('#trim').click()
    assert .059 < page.evaluate('buffer.duration') < .061
    page.locator('#undo').click()
    for edit in ['fadeIn','fadeOut','normalize']:
        page.locator('#'+edit).click()
        assert page.locator('#undo').is_enabled()
        page.locator('#undo').click()
    for engine in ['Grain','Spectral']:
        page.locator('#engine'+engine).click()
        page.locator('#play').click()
        page.wait_for_function('!$("stop").disabled')
        if engine=='Spectral': page.wait_for_function('!!spectralSource')
        page.locator('#stop').click()
        for seconds in [10,30,60]: export(page,root,engine,seconds)
    assert page.locator('#spectralFreeze').is_visible()
    for stretch in ['64','128']:
        page.locator('#extremeStretch').select_option(stretch)
        result=page.evaluate('async()=>{const r=await getSpectral();return r.audio.duration/r.rate}')
        assert abs(result-.1*int(stretch)) < .01
        print('PASS stretch',stretch)
    page.locator('#spectralFreeze').click()
    page.locator('#droneLoop').click()
    page.locator('#play').click()
    page.wait_for_function('!!spectralSource')
    duration=page.evaluate('spectralSource.buffer.duration/spectralSource.playbackRate.value')
    page.wait_for_timeout(int((duration*2+.1)*1000))
    assert page.evaluate('!!spectralSource && playing && spectralSource.loop')
    page.locator('#stop').click()
    assert not page.evaluate('!!spectralSource')
    print('PASS drone remains active across two loop boundaries')
    fingerprints=[]
    for position in [0,50,100]:
        page.locator('#freezePosition').fill(str(position))
        fingerprints.append(page.evaluate('async()=>{const r=await getSpectral();const a=r.audio.getChannelData(0);return Array.from(a.slice(100,120))}'))
    assert fingerprints[0]!=fingerprints[1]!=fingerprints[2]
    page.locator('#freezePosition').fill('15')
    for seconds in [10,30,60]: export(page,root,'freeze',seconds)
    page.locator('#droneLoop').click()
    export(page,root,'freeze',10)  # Export sustains even without live looping.
    # Pitch/reverb remain active.
    page.locator('#pitch').fill('12')
    page.locator('#reverb').fill('70')
    export(page,root,'freeze',10)
    # Deterministically cancel while the cooperative analysis is yielding.
    page.evaluate('invalidateSpectral();window.cancelJob=getSpectral().then(()=>false,e=>e.message.includes("中止"));stopPlayback()')
    assert page.evaluate('async()=>await window.cancelJob')
    assert not page.evaluate('spectralBusy')
    assert page.evaluate('async()=>!!(await getSpectral()).audio')
    print('PASS cancellation and reanalysis')
    assert page.evaluate("""async()=>{const old=buffer;buffer=ctx.createBuffer(2,44100,44100);$('pitch').value=24;$('spectralFreeze').click();$('extremeStretch').value=128;try{await getSpectral();return false}catch(e){return e.message.includes('32MB')}finally{buffer=old;$('pitch').value=0;$('spectralFreeze').click()}}""")
    print('PASS analysis memory guard')
    for filename in ['tiny.wav','silence.wav']:
        page.locator('#file').set_input_files(root/filename)
        page.wait_for_function('!spectralBusy && !!buffer && buffer.duration<.11')
        # Wait for async decode status, not just the previous buffer.
        page.wait_for_function('(name)=>$("recordStatus").textContent.includes(name)',arg=filename)
        export(page,root,'silent' if filename=='silence.wav' else 'tiny',10,filename=='silence.wav')
    # Regression for iPhone recordings decoded as left-only / right-only stereo.
    assert page.evaluate('''()=>{for(const c of [0,1]){const b=ctx.createBuffer(2,100,44100);b.getChannelData(c).fill(.4);const r=microphoneToDualMono(b);if(Math.abs(r.getChannelData(0)[0]-.4)>1e-6||r.getChannelData(0)[0]!==r.getChannelData(1)[0])return false}return true}''')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    assert not errors, errors
    browser.close()
    print('PASS edits, position, pitch/reverb, silence, one-sample input, dual-mono regression, mobile layout, no JS runtime errors')
