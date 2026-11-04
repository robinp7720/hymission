"""Rendered workspace selection and click geometry in a private compositor."""
import json
from pathlib import Path
import socket
import subprocess
import threading
import time

from PIL import Image


def run(request, root, env, runtime, processes, handles):
    def evaluate(code):
        reply = request('/eval ' + code)
        assert reply.strip() == 'ok', (code, reply)

    def state():
        return json.loads(request('j/hymission-overview-state'))

    def wait(predicate):
        for _ in range(200):
            current = state()
            if predicate(current):
                return current
            time.sleep(.02)
        raise AssertionError(current)

    def selected(current, workspace):
        entries = current.get('strip', [])
        return current['phase'] == 'active' and any(
            e['workspace'] == workspace and e['active'] and e['selection'] == 1
            for e in entries)

    child = env.copy()
    child.pop('LD_PRELOAD', None)
    child['WAYLAND_DISPLAY'] = next(p.name for p in runtime.glob('wayland-*') if p.is_socket())
    flags = subprocess.check_output(['pkg-config', '--cflags', '--libs', 'gtk4', 'gtk4-layer-shell-0'], text=True).split()
    bar = root / 'bar'
    subprocess.run(['cc', str(Path(__file__).with_name('search_reserved_test_bar.c')), *flags, '-o', str(bar)], check=True)
    log = open(root / 'bar.log', 'w')
    handles.append(log)
    processes.append(subprocess.Popen([str(bar)], env=child, stdout=log, stderr=subprocess.STDOUT))

    evaluate('hl.monitor({output="",mode="1280x720@60",position="auto",scale="1"}); '
             'hl.config({animations={enabled=true},input={follow_mouse=0},'
             'plugin={hymission={tiles_enabled=0,stage_enabled=0,search_enabled=0,'
             'workspace_strip_thickness=160,workspace_strip_active_scale=1.12,debug_logs=1,'
             'workspace_change_keeps_overview=1,hide_bar_when_strip=0,'
             'overview_focus_follows_mouse=0,workspace_strip_empty_mode="existing"}}})')
    for workspace in (701, 702, 703):
        evaluate(f'hl.workspace_rule({{workspace="{workspace}",persistent=true}}); '
                 f'hl.dispatch(hl.dsp.focus({{workspace={workspace}}}))')
        if workspace != 703:
            log = open(root / f'client-{workspace}.log', 'w')
            handles.append(log)
            client = subprocess.Popen(['weston-simple-shm'], env=child, stdout=log, stderr=subprocess.STDOUT)
            processes.append(client)
            for _ in range(100):
                if any(c['pid'] == client.pid for c in json.loads(request('j/clients'))):
                    break
                time.sleep(.02)
            else:
                raise AssertionError(f'workspace {workspace} client did not map')
    for _ in range(100):
        if json.loads(request('j/monitors'))[0]['reserved'][1] == 48:
            break
        time.sleep(.02)
    else:
        raise AssertionError('exclusive bar did not reserve its top edge')

    results = []
    for anchor in ('top', 'left', 'right'):
        evaluate(f'hl.config({{plugin={{hymission={{workspace_strip_anchor="{anchor}"}}}}}}); '
                 'hl.dispatch(hl.dsp.focus({workspace=701})); '
                 'hl.plugin.hymission.open("onlycurrentworkspace")')
        initial = wait(lambda s: selected(s, 701))
        target = next(e for e in initial['strip'] if e['workspace'] == 702)
        rect = target['visual_geometry']
        x, y = rect['x'] + rect['width'] / 2, rect['y'] + rect['height'] / 2
        prefix = root / (anchor + '-switch')
        evaluate(f'hl.plugin.hymission_tiles_test.pointer("move",{x},{y})')
        time.sleep(.1)
        evaluate('hl.plugin.hymission_tiles_test.frame_capture("begin")')
        evaluate(f'assert(hl.plugin.hymission_tiles_test.pointer("press",{x},{y}))')
        time.sleep(.03)
        evaluate(f'assert(hl.plugin.hymission_tiles_test.pointer("release",{x},{y}))')
        final = wait(lambda s: selected(s, 702))
        time.sleep(.08)
        evaluate('hl.plugin.hymission_tiles_test.frame_capture("end",' + json.dumps(str(prefix)) + ')')
        frames = json.loads(prefix.with_suffix('.json').read_text())
        values = [e['selection'] for f in frames for e in f['overview'].get('strip', []) if e['workspace'] == 702]
        assert any(.05 < value < .95 for value in values), (anchor, values)
        active = next(e for e in final['strip'] if e['workspace'] == 702)
        inactive = next(e for e in final['strip'] if e['workspace'] == 701)
        # The indicator fades with the selection: the default active tint is
        # rgba(5794f21a) and the inactive tint is transparent.
        tint_alphas = [e['indicator']['tint'][3] for f in frames for e in f['overview'].get('strip', []) if e['workspace'] == 702]
        settled = active['indicator']['tint'][3]
        assert abs(settled - 0x1a / 255) < .002 and inactive['indicator']['tint'][3] < .002, (anchor, active, inactive)
        assert any(.1 * settled < a < .9 * settled for a in tint_alphas), (anchor, tint_alphas)
        assert all(b >= a - 1e-6 for a, b in zip(tint_alphas, tint_alphas[1:])), (anchor, tint_alphas)
        ratio = active['visual_geometry']['width'] / inactive['visual_geometry']['width']
        assert abs(ratio - 1.12) < .001, (anchor, ratio)
        for frame in frames:
            for entry in frame['overview'].get('strip', []):
                slot, visible = entry['geometry'], entry['visual_geometry']
                assert visible['x'] >= slot['x'] - .01 and visible['y'] >= slot['y'] - .01
                assert visible['x'] + visible['width'] <= slot['x'] + slot['width'] + .01
                assert visible['y'] + visible['height'] <= slot['y'] + slot['height'] + .01
                assert visible['y'] >= 48 - .01, (anchor, entry)
            source = Path(frame['pixels'])
            png = source.with_suffix('.png')
            Image.open(source).save(png)
            source.unlink()
            frame['pixels'] = str(png)
        prefix.with_suffix('.json').write_text(json.dumps(frames, indent=2))
        subprocess.run(['grim', str(root / (anchor + '-selected.png'))], env=child, check=True, capture_output=True)
        results.append(dict(anchor=anchor, frames=len(frames), active_size_ratio=ratio, intermediate_frames=sum(.05 < v < .95 for v in values),
                            intermediate_indicator_frames=sum(.1 * settled < a < .9 * settled for a in tint_alphas)))

        # Reduced motion updates selection immediately, including on a rebuild.
        evaluate('hl.config({animations={enabled=false}}); hl.dispatch(hl.dsp.focus({workspace=703}))')
        reduced = wait(lambda s: selected(s, 703))
        assert all(e['selection'] in (0, 1) for e in reduced['strip'])
        assert all(min(abs(e['indicator']['tint'][3]), abs(e['indicator']['tint'][3] - settled)) < 1e-6
                   for e in reduced['strip'] if not e['new_workspace']), reduced['strip']
        evaluate('hl.plugin.hymission.close()')
        wait(lambda s: not s['active'])
        evaluate('hl.config({animations={enabled=true}})')

    # A held in-overview swipe moves the indicator with the fingers, a cancel
    # brings it back, and a completed swipe lands without replaying the ease.
    # Bars (Vigil) get the same live progress on socket2 that
    # tools/hypr-swipe-events publishes for native swipes.
    events = []
    bus = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    bus.connect(str(next((runtime / 'hypr').glob('*/.socket2.sock'))))
    handles.append(bus)

    def listen():
        pending = b''
        while chunk := bus.recv(65536):
            pending += chunk
            *lines, pending = pending.split(b'\n')
            events.extend(line.decode(errors='replace') for line in lines)
    threading.Thread(target=listen, daemon=True).start()

    def swipe_events():
        return [e for e in events if e.split('>>')[0] in ('workspace', 'workspaceswipe', 'workspaceswipeend')]

    def progress(event):
        return float(event.split(',')[-1])

    def sel(current, workspace):
        return next(e['selection'] for e in current['strip'] if e['workspace'] == workspace)

    def swipe(dx):
        for _ in range(4):
            evaluate(f'hl.plugin.hymission_tiles_test.trackpad("update",0,{dx / 4})')
            time.sleep(.02)

    evaluate('hl.config({gestures={workspace_swipe_invert=false,workspace_swipe_distance=300,'
             'workspace_swipe_cancel_ratio=.5,workspace_swipe_min_speed_to_force=0,workspace_swipe_forever=false}}); '
             'hl.gesture({fingers=4,direction="horizontal",action="workspace"}); '
             'hl.dispatch(hl.dsp.focus({workspace=701})); hl.plugin.hymission.open("onlycurrentworkspace")')
    wait(lambda s: selected(s, 701))
    time.sleep(.1)
    events.clear()
    evaluate('hl.plugin.hymission_tiles_test.trackpad("begin")')
    swipe(120)
    held = state()
    time.sleep(.25)
    still = state()
    tracked = dict(source=sel(held, 701), target=sel(held, 702), later_target=sel(still, 702))
    assert json.loads(request('j/activeworkspace'))['id'] == 701, tracked
    assert .1 < tracked['target'] < .9 and abs(tracked['source'] + tracked['target'] - 1) < 1e-6, tracked
    assert abs(tracked['later_target'] - tracked['target']) < 1e-6, ('held swipe must not drift', tracked)
    live = [e for e in swipe_events() if e.startswith('workspaceswipe>>')]
    assert live and live[-1].split('>>')[1].split(',')[1:3] == ['701', '702'], swipe_events()
    assert abs(progress(live[-1]) - tracked['target']) < .01, ('bar progress must match the overview', live[-1], tracked)
    swipe(60)
    assert sel(state(), 702) > tracked['target'] + .05, ('indicator must follow further movement', tracked)
    evaluate('hl.plugin.hymission_tiles_test.trackpad("cancel")')
    wait(lambda s: selected(s, 701) and sel(s, 702) == 0)
    time.sleep(.1)
    cancelled = swipe_events()
    assert cancelled[-1].startswith('workspaceswipeend>>') and progress(cancelled[-2]) < .05, cancelled
    assert not any(e.startswith('workspace>>') for e in cancelled), ('a cancelled swipe must not switch', cancelled)
    events.clear()
    evaluate('hl.plugin.hymission_tiles_test.trackpad("begin")')
    swipe(240)
    evaluate('hl.plugin.hymission_tiles_test.trackpad("end")')
    landed = wait(lambda s: selected(s, 702))
    assert sel(landed, 701) == 0, ('commit must not replay the selection ease', landed['strip'])
    time.sleep(.1)
    committed = swipe_events()
    # Native order: the new workspace first, then the end, then nothing.
    assert committed[-2:] == ['workspace>>702', committed[-1]] and committed[-1].startswith('workspaceswipeend>>'), committed
    assert progress(committed[-3]) > .99, committed
    tracked['landed'] = True
    results.append(dict(gesture=tracked))
    evaluate('hl.plugin.hymission.close()')
    wait(lambda s: not s['active'])

    evaluate('hl.config({plugin={hymission={workspace_strip_active_scale=1}}}); hl.plugin.hymission.open("onlycurrentworkspace")')
    equal = wait(lambda s: s['phase'] == 'active')
    assert all(e['geometry'] == e['visual_geometry'] for e in equal['strip'])
    evaluate('hl.plugin.hymission.close()')
    wait(lambda s: not s['active'])
    errors = request('/configerrors').strip()
    assert errors in ('', 'ok'), errors
    (root / 'results.json').write_text(json.dumps(results, indent=2))
    print('PASS: animated workspace selection and indicator, gesture-tracked selection and socket2 swipe progress, three anchors, visible click targets, bar reservation, reduced motion, equal-size opt-out')
