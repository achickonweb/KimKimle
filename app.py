from flask import Flask, render_template_string, request
from flask_socketio import SocketIO, join_room, emit
import random, string, threading

app = Flask(__name__)
app.config['SECRET_KEY'] = 'party_secret_v9'
socketio = SocketIO(app, cors_allowed_origins="*")

TOTAL_ROUNDS  = 3
ANSWER_TIME   = 20
REJOIN_GRACE  = 30   # seconds a disconnected slot is held

GAME_MODES = {
    'classic': {
        'label': 'Klasik Parti', 'theme': 'theme-indigo', 'q_count': 7,
        'questions': ["Kim?","Kiminle?","Nerede?","Ne zaman?","Ne yapıyor?","Kim gördü?","Ne dedi?"],
        'story_template': "{0}, {1} ile {2}'da, {3} {4}. Bunu gören {5}, '{6}' dedi."
    },
    'horror': {
        'label': 'Korku Evi 👻', 'theme': 'theme-red', 'q_count': 7,
        'questions': ["Hangi kurban?","Kimin cesediyle?","Hangi lanetli yerde?","Gece saat kaçta?",
                      "Nasıl öldürüyor?","Hangi yaratık gördü?","Son sözü neydi?"],
        'story_template': "{0}, {1} cesediyle {2}'da, {3} {4}. {5} aniden belirdi ve fısıldadı: '{6}'"
    },
    'scifi': {
        'label': 'Cyberpunk 🤖', 'theme': 'theme-cyan', 'q_count': 7,
        'questions': ["Hangi Cyborg?","Hangi yapay zekayla?","Hangi gezegende?","Hangi yılda?",
                      "Hangi hack'i yapıyor?","Hangi drone kaydetti?","Sistem hatası neydi?"],
        'story_template': "{0}, {1} model android ile {2}'da, {3} yılında {4}. {5} verileri işledi ve kod çıktı: '{6}'"
    },
    'parallel': {
        'label': 'PARALEL EVREN 🌌', 'theme': 'theme-parallel', 'q_count': 7,
        'questions': ["Kim?","Kiminle?","Nerede?","Ne zaman?","Ne yapıyor?","Kim gördü?","Ne dedi?"],
        'story_template': "{0}, {1} ile {2}'da, {3} {4}. Olayı {5} izliyordu ve bağırdı: '{6}'"
    },
    'absurd': {
        'label': 'Tamamen Kaos 🌀', 'theme': 'theme-purple', 'q_count': 7,
        'questions': ["Ne dedi?","Nerede?","Kim?","Ne zaman?","Kiminle?","Ne yapıyor?","Kim gördü?"],
        'story_template': "Önce '{0}' dedi. Sonra {1}'da, {2}, {3} vakti {4} ile {5}. En son {6} şahit oldu."
    },
    'uzatilmis': {
        'label': 'Uzatılmış ⏳', 'theme': 'theme-orange', 'q_count': 12,
        'questions': [
            "Kim?","Kimle?","Nerede?","Ne zaman?","Neden?","Ne yapıyor?",
            "Nasıl yapıyor?","Elinde ne tutuyor?","Kim görüyor?","Ne diyor?",
            "Kim engelliyor?","Ne düşünüyor?"
        ],
        'story_template': (
            "{0}, {1} ile {2}'da {3} vakti, {4} için {5}. "
            "Bunu {6} şekilde yapıyor, elinde {7} tutuyor. "
            "{8} görüyor ve '{9}' diyor. "
            "Tam o sırada {10} engelliyor. {0} ise '{11}' diye düşünüyor."
        )
    },
    'custom': {
        'label': 'Özel Sorular ✏️', 'theme': 'theme-indigo', 'q_count': 7,
        'questions': [],
        'story_template': ''
    }
}

rooms = {}

# ── helpers ───────────────────────────────────────────────────────────────────

def make_room_state(sid, name, avatar):
    return {
        'players':   [{'id': sid, 'name': name, 'avatar': avatar, 'is_spectator': False, 'disconnected': False}],
        'answers': [], 'attributed_answers': [],
        'step': 0, 'round': 0,
        'scores': {}, 'stories': [], 'attributed_stories': [],
        'voted_players': set(), 'answer_votes': {},
        'settings': {'mode': 'classic', 'show_author': False,
                     'custom_questions': [], 'custom_template': ''},
        'parallel_state': {'phase':'idle','round_answers':{},'round_votes':{},'candidates':[]},
        'host_id': sid,
        'game_active': False,
        'grace_timers': {}   # sid -> threading.Timer
    }

def active_players(c):
    return [p for p in rooms[c]['players'] if not p.get('is_spectator') and not p.get('disconnected')]

def get_scores_display(c):
    r = rooms[c]
    result = [{'id':p['id'],'name':p['name'],'avatar':p['avatar'],
               'score':r['scores'].get(p['id'],0),'is_spectator':p.get('is_spectator',False)}
              for p in r['players']]
    return sorted(result, key=lambda x: x['score'], reverse=True)

def mode_questions(c):
    r = rooms[c]; mode = r['settings']['mode']
    if mode == 'custom':
        qs = r['settings'].get('custom_questions', [])
        return qs if qs else ["Kim?","Nerede?","Ne yaptı?","Ne dedi?","Kim gördü?","Sonuç?","Moral?"]
    return GAME_MODES[mode]['questions']

def mode_q_count(c):
    r = rooms[c]; mode = r['settings']['mode']
    if mode == 'custom':
        qs = r['settings'].get('custom_questions', [])
        return len(qs) if qs else 7
    return GAME_MODES[mode]['q_count']

def build_story(c):
    r = rooms[c]; mode = r['settings']['mode']
    ans = r['answers']
    if mode == 'custom':
        tmpl = r['settings'].get('custom_template', '')
        if tmpl:
            try: return tmpl.format(*ans)
            except: pass
        return ' '.join(f"[{q}]: {a}" for q,a in zip(mode_questions(c), ans))
    tmpl = GAME_MODES[mode]['story_template']
    padded = ans + ['...'] * (12 - len(ans))
    try: return tmpl.format(*padded)
    except: return ' '.join(str(a) for a in ans)

def start_new_round(c):
    r = rooms[c]
    r['answers'] = []; r['attributed_answers'] = []
    r['step'] = 0; r['voted_players'] = set(); r['answer_votes'] = {}
    r['parallel_state'] = {'phase':'idle','round_answers':{},'round_votes':{},'candidates':[]}
    emit('round_start', {'round': r['round']+1, 'total': TOTAL_ROUNDS,
                         'q_count': mode_q_count(c), 'mode': r['settings']['mode']}, room=c)
    if r['settings']['mode'] == 'parallel':
        start_parallel_round(c)
    else:
        send_classic_turn(c)

# ── routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index(): return render_template_string(HTML_TEMPLATE)

# ── socket: room ──────────────────────────────────────────────────────────────

@socketio.on('create_room')
def create(d):
    code = ''.join(random.choices(string.ascii_uppercase, k=4))
    rooms[code] = make_room_state(request.sid, d['name'], d['avatar'])
    join_room(code)
    emit('room_created', {'room': code, 'is_host': True})
    emit('update_list', rooms[code]['players'], room=code)

@socketio.on('join_room')
def join(d):
    c = d['room']
    is_spec = d.get('spectator', False)
    if c in rooms:
        rooms[c]['players'].append({'id':request.sid,'name':d['name'],
                                    'avatar':d['avatar'],'is_spectator':is_spec,
                                    'disconnected': False})
        join_room(c)
        is_host = rooms[c]['host_id'] == request.sid
        emit('room_created', {'room': c, 'spectator': is_spec, 'is_host': is_host})
        emit('update_list', rooms[c]['players'], room=c)
        s = rooms[c]['settings']
        emit('settings_changed',{'key':'mode','val':s['mode'],'config':GAME_MODES.get(s['mode'],GAME_MODES['classic'])},room=request.sid)
        emit('settings_changed',{'key':'show_author','val':s['show_author']},room=request.sid)
        if s.get('custom_questions'):
            emit('settings_changed',{'key':'custom_questions','val':s['custom_questions'],'template':s.get('custom_template','')},room=request.sid)
    else:
        emit('err','Oda bulunamadı')

@socketio.on('update_settings')
def upd_set(d):
    c = d['room']
    if c not in rooms: return
    rooms[c]['settings'][d['key']] = d['val']
    payload = {'key': d['key'], 'val': d['val']}
    if d['key'] == 'mode':
        payload['config'] = GAME_MODES.get(d['val'], GAME_MODES['classic'])
    if d['key'] == 'custom_questions':
        rooms[c]['settings']['custom_template'] = d.get('template','')
        payload['template'] = d.get('template','')
    emit('settings_changed', payload, room=c)

@socketio.on('start_game')
def start(d):
    c = d['room']
    r = rooms[c]
    r['round']=0; r['scores']={}; r['stories']=[]; r['attributed_stories']=[]; r['game_active']=True
    emit('game_start',{'mode':r['settings']['mode'],'total_rounds':TOTAL_ROUNDS,
                       'q_count':mode_q_count(c)},room=c)
    start_new_round(c)

# ── skip question (host only) ─────────────────────────────────────────────────

@socketio.on('skip_question')
def skip_question(d):
    c = d['room']
    if c not in rooms: return
    r = rooms[c]
    if r['host_id'] != request.sid: return
    players = active_players(c)
    if not players: return
    p = players[r['step'] % len(players)]
    qs = mode_questions(c)
    r['answers'].append('...')
    r['attributed_answers'].append({
        'text':'...','owner_id':p['id'],
        'owner_name':p['name'],'owner_avatar':'⏭️',
        'question':qs[r['step']]
    })
    r['step'] += 1
    emit('question_skipped', {'step': r['step'], 'skipped_player': p['name']}, room=c)
    if r['step'] < mode_q_count(c): send_classic_turn(c)
    else: finish_game(c)

# ── classic ────────────────────────────────────────────────────────────────────

def send_classic_turn(c):
    r = rooms[c]
    qs = mode_questions(c)
    players = active_players(c)
    if not players: return
    p = players[r['step'] % len(players)]
    emit('turn_data',{
        'step':r['step'],'q':qs[r['step']],'timer':ANSWER_TIME,
        'total_q':mode_q_count(c),
        'active_id':p['id'],'active_name':p['name'],'active_avatar':p['avatar'],
        'host_id':r['host_id']
    },room=c)

@socketio.on('submit_ans')
def classic_ans(d):
    c = d['room']; r = rooms[c]
    players = active_players(c)
    p = players[r['step'] % len(players)]
    qs = mode_questions(c)
    r['answers'].append(d['ans'])
    r['attributed_answers'].append({
        'text':d['ans'],'owner_id':p['id'],
        'owner_name':p['name'],'owner_avatar':p['avatar'],
        'question':qs[r['step']]
    })
    r['step'] += 1
    if r['step'] < mode_q_count(c): send_classic_turn(c)
    else: finish_game(c)

# ── parallel ───────────────────────────────────────────────────────────────────

def start_parallel_round(c):
    r = rooms[c]
    qs = mode_questions(c)
    r['parallel_state'].update({'phase':'answering','round_answers':{},'round_votes':{},'candidates':[]})
    emit('p_round_start',{'step':r['step'],'q':qs[r['step']],
                          'timer':ANSWER_TIME,'total_q':mode_q_count(c)},room=c)

@socketio.on('submit_parallel_ans')
def p_ans(d):
    c = d['room']; r = rooms[c]
    r['parallel_state']['round_answers'][request.sid] = d['ans']
    players = active_players(c)
    count = len(r['parallel_state']['round_answers'])
    total = len(players)
    emit('p_answer_count', {'count': count, 'total': total}, room=c)
    if count >= total:
        prepare_voting(c)

def prepare_voting(c, tie_candidates=None):
    r = rooms[c]
    r['parallel_state']['phase']='voting'; r['parallel_state']['round_votes']={}
    show_names = r['settings'].get('show_author',False)
    if tie_candidates:
        candidates = tie_candidates
    else:
        candidates=[]
        for pid,text in r['parallel_state']['round_answers'].items():
            author=next((p['name'] for p in r['players'] if p['id']==pid),'???')
            candidates.append({'owner_id':pid,'text':text,'name':author if show_names else None})
    r['parallel_state']['candidates']=candidates
    random.shuffle(candidates)
    emit('p_vote_start',{'candidates':candidates,'is_tie':tie_candidates is not None},room=c)

@socketio.on('cast_vote')
def p_vote(d):
    c = d['room']; r = rooms[c]
    r['parallel_state']['round_votes'][request.sid]=d['candidate_id']
    players = active_players(c)
    if len(r['parallel_state']['round_votes'])>=len(players):
        calculate_parallel_results(c)

def calculate_parallel_results(c):
    r = rooms[c]
    tally={cand['owner_id']:0 for cand in r['parallel_state']['candidates']}
    for t in r['parallel_state']['round_votes'].values():
        if t in tally: tally[t]+=1
    max_v=max(tally.values()) if tally else 0
    winners=[cid for cid,cnt in tally.items() if cnt==max_v]
    if len(winners)==1:
        wid=winners[0]
        wp=next((p for p in r['players'] if p['id']==wid),None)
        qs=mode_questions(c)
        text=r['parallel_state']['round_answers'][wid]
        r['answers'].append(text)
        r['attributed_answers'].append({
            'text':text,'owner_id':wid,
            'owner_name':wp['name'] if wp else '???',
            'owner_avatar':wp['avatar'] if wp else '❓',
            'question':qs[r['step']]
        })
        r['step']+=1
        if r['step']<mode_q_count(c): start_parallel_round(c)
        else: finish_game(c)
    else:
        prepare_voting(c,tie_candidates=[cand for cand in r['parallel_state']['candidates'] if cand['owner_id'] in winners])

# ── post-story ─────────────────────────────────────────────────────────────────

def finish_game(c):
    r = rooms[c]
    story = build_story(c)
    r['stories'].append(story)
    r['attributed_stories'].append({
        'story': story,
        'attributed_answers': r['attributed_answers'][:]
    })
    emit('story_reveal',{
        'story':story,'attributed_answers':r['attributed_answers'],
        'round':r['round']+1,'total_rounds':TOTAL_ROUNDS
    },room=c)

@socketio.on('send_emoji')
def emoji_react(d):
    emit('emoji_broadcast',{
        'emoji':d['emoji'],
        'is_spectator':d.get('is_spectator', False)
    },room=d['room'])

@socketio.on('send_phrase')
def phrase_react(d):
    c = d.get('room')
    if c not in rooms: return
    emit('phrase_broadcast', {
        'text': d.get('text','')[:60],
        'name': d.get('name',''),
        'avatar': d.get('avatar','😶')
    }, room=c, include_self=False)

@socketio.on('submit_answer_vote')
def answer_vote(d):
    c = d['room']; r = rooms[c]
    voter, target = request.sid, d['voted_for']
    category = d.get('category', '😂')
    r['voted_players'].add(voter)
    if voter != target:
        r['answer_votes'][voter] = {'target': target, 'category': category}
    players = active_players(c)
    if len(r['voted_players']) >= len(players):
        tally = {}
        cat_tally = {}
        for v in r['answer_votes'].values():
            t = v['target']
            tally[t] = tally.get(t, 0) + 1
            if t not in cat_tally: cat_tally[t] = {}
            cat = v['category']
            cat_tally[t][cat] = cat_tally[t].get(cat, 0) + 1
        winner_id = max(tally, key=tally.get) if tally else None
        winner_category = None
        if winner_id and winner_id in cat_tally:
            winner_category = max(cat_tally[winner_id], key=cat_tally[winner_id].get)
        if winner_id: r['scores'][winner_id] = r['scores'].get(winner_id, 0) + 1
        r['voted_players'] = set(); r['answer_votes'] = {}
        emit('vote_result',{
            'winner_id': winner_id,
            'winner_category': winner_category,
            'scores': get_scores_display(c),
            'round': r['round'], 'total_rounds': TOTAL_ROUNDS
        }, room=c)

@socketio.on('next_round')
def next_round_handler(d):
    c = d['room']; r = rooms[c]
    r['round'] += 1
    if r['round'] >= TOTAL_ROUNDS:
        r['game_active'] = False
        emit('game_final',{
            'scores': get_scores_display(c),
            'stories': r['stories'],
            'attributed_stories': r['attributed_stories']
        }, room=c)
    else:
        emit('show_scores',{
            'scores': get_scores_display(c),
            'round': r['round'],
            'mode': r['settings']['mode']
        }, room=c)

@socketio.on('confirm_next_round')
def confirm_next(d):
    c = d['room']
    if c not in rooms: return
    if 'new_mode' in d and d['new_mode']:
        rooms[c]['settings']['mode'] = d['new_mode']
        emit('settings_changed',{
            'key':'mode','val':d['new_mode'],
            'config':GAME_MODES.get(d['new_mode'],GAME_MODES['classic'])
        }, room=c)
    start_new_round(c)

# ── disconnect / reconnect grace period ──────────────────────────────────────

def _remove_player(room_code, sid):
    """Called after grace period if player never came back."""
    if room_code not in rooms: return
    r = rooms[room_code]
    p = next((x for x in r['players'] if x['id'] == sid), None)
    if not p: return
    # Only hard-remove if still flagged disconnected
    if not p.get('disconnected'): return
    r['players'] = [x for x in r['players'] if x['id'] != sid]
    r['grace_timers'].pop(sid, None)
    # If host left, promote next active player
    if r['host_id'] == sid:
        remaining = [x for x in r['players'] if not x.get('is_spectator')]
        if remaining:
            r['host_id'] = remaining[0]['id']
            socketio.emit('host_transfer', {'new_host_id': remaining[0]['id'],
                                            'new_host_name': remaining[0]['name']}, room=room_code)
    socketio.emit('update_list', r['players'], room=room_code)
    socketio.emit('player_left', {'name': p['name'], 'avatar': p['avatar']}, room=room_code)
    # Clean up empty rooms
    if not r['players']:
        rooms.pop(room_code, None)

@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    for code, r in list(rooms.items()):
        p = next((x for x in r['players'] if x['id'] == sid), None)
        if not p: continue
        p['disconnected'] = True
        # Cancel any existing timer for this sid
        old = r['grace_timers'].pop(sid, None)
        if old: old.cancel()
        # Notify room
        emit('player_disconnect', {'name': p['name'], 'avatar': p['avatar'],
                                   'grace': REJOIN_GRACE}, room=code)
        emit('update_list', r['players'], room=code)
        # Start grace timer
        t = threading.Timer(REJOIN_GRACE, _remove_player, args=[code, sid])
        t.daemon = True
        r['grace_timers'][sid] = t
        t.start()
        break

@socketio.on('rejoin_room')
def rejoin(d):
    c = d['room']
    old_sid = d.get('old_sid', '')
    if c not in rooms:
        emit('err', 'Oda artık mevcut değil'); return
    r = rooms[c]
    # Cancel grace timer if it's still running
    old_timer = r['grace_timers'].pop(old_sid, None)
    if old_timer: old_timer.cancel()
    existing = next((p for p in r['players'] if p['id'] == old_sid), None)
    if existing:
        existing['id'] = request.sid
        existing['disconnected'] = False
        if old_sid in r['scores']:
            r['scores'][request.sid] = r['scores'].pop(old_sid)
        if r['host_id'] == old_sid:
            r['host_id'] = request.sid
    else:
        r['players'].append({'id':request.sid,'name':d['name'],'avatar':d.get('avatar','🐱'),'is_spectator':False,'disconnected':False})
    join_room(c)
    is_host = r['host_id'] == request.sid
    emit('room_created', {'room': c, 'spectator': False, 'is_host': is_host, 'rejoin': True})
    emit('update_list', r['players'], room=c)
    s = r['settings']
    emit('settings_changed',{'key':'mode','val':s['mode'],'config':GAME_MODES.get(s['mode'],GAME_MODES['classic'])},room=request.sid)
    emit('player_rejoin', {'name': existing['name'] if existing else d['name']}, room=c)

# ── kick player (host only, lobby only) ──────────────────────────────────────

@socketio.on('kick_player')
def kick_player(d):
    c = d['room']
    if c not in rooms: return
    r = rooms[c]
    if r['host_id'] != request.sid: return
    target_id = d['target_id']
    target = next((p for p in r['players'] if p['id'] == target_id), None)
    if not target: return
    r['players'] = [p for p in r['players'] if p['id'] != target_id]
    emit('you_were_kicked', room=target_id)
    emit('update_list', r['players'], room=c)
    emit('player_kicked', {'name': target['name']}, room=c)

# ── typing indicator ──────────────────────────────────────────────────────────

@socketio.on('typing')
def typing(d):
    c = d['room']
    if c not in rooms: return
    # Broadcast to everyone except sender
    emit('player_typing', {'player_id': request.sid, 'is_typing': d.get('is_typing', True)},
         room=c, include_self=False)

# ── HTML ──────────────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""{% raw %}<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Kim Kimle? — V8</title>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Unbounded:wght@400;900&family=Plus+Jakarta+Sans:wght@300;700&family=Press+Start+2P&family=Special+Elite&family=Fredoka+One&display=swap" rel="stylesheet">
<style>
:root{
  --primary:#6366f1;--primary-rgb:99,102,241;
  --bg:#020617;--surface:rgba(15,23,42,.92);
  --border:rgba(255,255,255,.1);--text:#f8fafc;--muted:#94a3b8;
  --radius:1rem;--font:'Plus Jakarta Sans',sans-serif;
}
body.ui-modern{--bg:#020617;--surface:rgba(15,23,42,.92);--border:rgba(255,255,255,.1);--text:#f8fafc;--muted:#94a3b8;}
body.ui-modern #main-app{background:var(--surface);backdrop-filter:blur(20px);}
body.ui-retro{
  --bg:#0d0221;--surface:#1a0533;--border:#ff00ff;--text:#00ff41;--muted:#aa44aa;
  font-family:'Press Start 2P',monospace!important;image-rendering:pixelated;
}
body.ui-retro #main-app{background:var(--surface);border:4px solid #ff00ff!important;box-shadow:6px 6px 0 #ff00ff,12px 12px 0 #00ff41;border-radius:0!important;}
body.ui-retro .card{border-radius:0!important;border:3px solid var(--border)!important;}
body.ui-retro button{border-radius:0!important;border:2px solid currentColor!important;}
body.ui-retro input,body.ui-retro textarea{border-radius:0!important;border:2px solid var(--border)!important;}
body.ui-retro h1{text-shadow:3px 3px #ff00ff,6px 6px #00ff41;}
body.ui-retro .scanline{display:block;}
body.ui-newspaper{
  --bg:#f5f0e8;--surface:#fffdf7;--border:#1a1a1a;--text:#1a1a1a;--muted:#555;
  font-family:'Special Elite',serif!important;
}
body.ui-newspaper #main-app{background:var(--surface);border:3px double #1a1a1a!important;box-shadow:4px 4px 0 #1a1a1a;border-radius:0!important;}
body.ui-newspaper .card{border:2px solid #1a1a1a!important;border-radius:0!important;background:#fffdf7!important;}
body.ui-newspaper button{border-radius:0!important;border:2px solid #1a1a1a!important;}
body.ui-newspaper input,body.ui-newspaper textarea{border-radius:0!important;border:2px solid #1a1a1a!important;background:#f5f0e8!important;color:#1a1a1a!important;}
body.ui-newspaper h1{font-family:'Special Elite',serif;letter-spacing:.05em;}
body.ui-newspaper .accent-text{color:#1a1a1a!important;}
body.ui-newspaper .accent-bg{background:#1a1a1a!important;}
body.ui-newspaper .bg-glow{display:none;}
body.ui-neon{
  --bg:#000008;--surface:#05000f;--border:#ff00ff;--text:#ffffff;--muted:#cc44ff;
  font-family:'Unbounded',sans-serif!important;
}
body.ui-neon #main-app{background:var(--surface);border:2px solid #ff00ff!important;box-shadow:0 0 20px #ff00ff,0 0 60px #7700ff,inset 0 0 30px rgba(255,0,255,.05);border-radius:0!important;}
body.ui-neon .card{border:1px solid #ff00ff!important;box-shadow:0 0 8px #ff00ff44!important;border-radius:0!important;}
body.ui-neon button{border-radius:0!important;}
body.ui-neon input,body.ui-neon textarea{border-radius:0!important;border:1px solid #ff00ff!important;background:#05000f!important;color:#fff!important;}
body.ui-neon h1{text-shadow:0 0 20px #ff00ff,0 0 40px #ff00ff;}
body.ui-neon .accent-text{color:#ff00ff!important;text-shadow:0 0 10px #ff00ff;}
body.ui-neon .accent-bg{background:#ff00ff!important;box-shadow:0 0 15px #ff00ff;}
body.ui-neon .scanline{display:block;}
body.ui-kawaii{
  --bg:#fce4ec;--surface:#fff0f5;--border:#f48fb1;--text:#4a0020;--muted:#ad6c80;
  font-family:'Fredoka One',sans-serif!important;
}
body.ui-kawaii #main-app{background:var(--surface);border:3px solid #f48fb1!important;border-radius:2rem!important;box-shadow:0 8px 32px rgba(244,143,177,.4);}
body.ui-kawaii .card{border-radius:1.5rem!important;border:2px solid #f8bbd0!important;background:#fff5f8!important;}
body.ui-kawaii button{border-radius:9999px!important;}
body.ui-kawaii input,body.ui-kawaii textarea{border-radius:1rem!important;border:2px solid #f48fb1!important;background:#fff!important;color:#4a0020!important;}
body.ui-kawaii h1{background:linear-gradient(135deg,#f06292,#ce93d8)!important;-webkit-background-clip:text!important;-webkit-text-fill-color:transparent!important;}
body.ui-kawaii .accent-text{color:#e91e63!important;}
body.ui-kawaii .accent-bg{background:linear-gradient(135deg,#f06292,#ce93d8)!important;}
body.ui-kawaii .bg-glow{background:radial-gradient(circle,rgba(240,98,146,.2),transparent)!important;}

body.game-indigo{--primary:#6366f1;--primary-rgb:99,102,241}
body.game-red{--primary:#ef4444;--primary-rgb:239,68,68}
body.game-cyan{--primary:#06b6d4;--primary-rgb:6,182,212}
body.game-purple{--primary:#d946ef;--primary-rgb:217,70,239}
body.game-parallel{--primary:#10b981;--primary-rgb:16,185,129}
body.game-orange{--primary:#f97316;--primary-rgb:249,115,22}

body{font-family:var(--font);background:var(--bg);color:var(--text);margin:0;min-height:100dvh;display:flex;align-items:center;justify-content:center;overflow-x:hidden;transition:background .4s,color .4s;}
#main-app{width:100%;max-width:600px;min-height:100dvh;display:flex;flex-direction:column;transition:all .4s;}
@media(min-width:640px){#main-app{height:90dvh;min-height:640px;border-radius:2rem;border:1px solid var(--border);}}
.accent-text{color:var(--primary)!important;}
.accent-bg{background-color:var(--primary)!important;}
.bg-glow{position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);width:150vw;height:150vh;pointer-events:none;z-index:-1;background:radial-gradient(circle,rgba(var(--primary-rgb),.12),transparent);transition:background .5s;}
.card{background:rgba(255,255,255,.05);border:1px solid var(--border);border-radius:var(--radius);}
.input-field{width:100%;background:rgba(0,0,0,.3);border:1px solid rgba(255,255,255,.15);padding:.9rem 1rem;border-radius:.75rem;color:var(--text);font-family:var(--font);font-size:1rem;font-weight:700;outline:none;transition:border-color .2s;}
.input-field:focus{border-color:var(--primary);}
.btn-primary{width:100%;background:var(--primary);color:#fff;padding:1rem;border-radius:.75rem;font-weight:900;font-size:1rem;border:none;cursor:pointer;transition:filter .15s,transform .1s;}
.btn-primary:hover{filter:brightness(1.1);}
.btn-primary:active{transform:scale(.97);}
.btn-secondary{background:rgba(255,255,255,.08);color:var(--text);padding:1rem;border-radius:.75rem;font-weight:700;border:none;cursor:pointer;transition:background .15s;}
.btn-secondary:hover{background:rgba(255,255,255,.14);}
.scanline{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,.08) 2px,rgba(0,0,0,.08) 4px);pointer-events:none;z-index:998;}
#timer-bar,#p-timer-bar{transition:width 1s linear,background-color .3s;}
.paper-sheet{background:#fff;color:#1e293b;border-radius:12px;padding:1.5rem;}
.paper-fly{animation:flyAway .7s forwards ease-in;}
@keyframes flyAway{to{transform:translateY(-700px) rotate(-12deg);opacity:0;}}
.story-word{display:inline;opacity:0;transition:opacity .35s ease;}
.story-word.vis{opacity:1;}
@keyframes floatUp{0%{transform:translateY(0) scale(1);opacity:1;}100%{transform:translateY(-65vh) scale(2);opacity:0;}}
.float-emoji{position:fixed;font-size:2.2rem;pointer-events:none;z-index:999;animation:floatUp 1.9s ease-out forwards;}
.float-emoji.spec{font-size:1.6rem;}
@keyframes confettiFall{0%{transform:translateY(-5vh) rotate(0deg);opacity:1;}100%{transform:translateY(105vh) rotate(720deg);opacity:0;}}
.confetti-piece{position:fixed;pointer-events:none;z-index:1000;animation:confettiFall linear forwards;}
@keyframes pageFlip{from{transform:perspective(800px) rotateY(-25deg);opacity:0;}to{transform:perspective(800px) rotateY(0deg);opacity:1;}}
.book-page{animation:pageFlip .5s ease-out forwards;}
.spec-badge{background:rgba(255,165,0,.15);border:1px solid rgba(255,165,0,.4);color:#fbbf24;padding:2px 8px;border-radius:999px;font-size:.6rem;font-weight:700;}
.toggle-checkbox:checked{right:0;border-color:#68D391;}
.toggle-checkbox:checked+.toggle-label{background-color:#68D391;}
.no-scrollbar::-webkit-scrollbar{display:none;}
@keyframes slideUp{from{transform:translateY(20px);opacity:0;}to{transform:translateY(0);opacity:1;}}
.slide-up{animation:slideUp .3s ease-out forwards;}
@keyframes pulse-ring{0%{transform:scale(.95);box-shadow:0 0 0 0 rgba(var(--primary-rgb),.4);}70%{transform:scale(1);box-shadow:0 0 0 10px rgba(var(--primary-rgb),0);}100%{transform:scale(.95);box-shadow:0 0 0 0 rgba(var(--primary-rgb),0);}}
.pulse-ring{animation:pulse-ring 2s infinite;}

/* typing dots */
@keyframes typingBounce{0%,80%,100%{transform:translateY(0)}40%{transform:translateY(-6px)}}
.typing-dot{width:6px;height:6px;border-radius:50%;background:var(--primary);display:inline-block;animation:typingBounce 1.2s infinite ease-in-out;}
.typing-dot:nth-child(2){animation-delay:.15s;}
.typing-dot:nth-child(3){animation-delay:.3s;}

/* disconnected badge */
.dc-badge{background:rgba(239,68,68,.15);border:1px solid rgba(239,68,68,.4);color:#fca5a5;padding:2px 7px;border-radius:999px;font-size:.55rem;font-weight:700;}

/* answer reveal animation */
@keyframes answerSlide{from{transform:translateX(-18px);opacity:0;}to{transform:translateX(0);opacity:1;}}
.answer-reveal{animation:answerSlide .4s ease-out forwards;opacity:0;}

/* quick phrases */
.phrase-btn{background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.12);color:var(--text);padding:.4rem .8rem;border-radius:9999px;font-size:.72rem;font-weight:700;cursor:pointer;transition:background .15s,transform .1s;white-space:nowrap;}
.phrase-btn:hover{background:rgba(255,255,255,.15);}
.phrase-btn:active{transform:scale(.93);}

/* floating phrase toast */
@keyframes phraseFloat{0%{transform:translateY(0);opacity:1;}100%{transform:translateY(-55px);opacity:0;}}
.phrase-toast{position:fixed;pointer-events:none;z-index:999;animation:phraseFloat 2.2s ease-out forwards;background:rgba(0,0,0,.75);backdrop-filter:blur(8px);border:1px solid rgba(255,255,255,.15);padding:.35rem .85rem;border-radius:9999px;font-size:.78rem;font-weight:700;color:#fff;white-space:nowrap;}
</style>
</head>
<body class="ui-modern game-indigo">
<div class="scanline"></div>
<div class="bg-glow"></div>

<!-- ══ QR CODE MODAL ══ -->
<div id="modal-qr" class="hidden fixed inset-0 z-[990] flex items-center justify-center p-4" style="background:rgba(0,0,0,.8);backdrop-filter:blur(10px)" onclick="if(event.target===this)hide('modal-qr')">
  <div class="card p-6 rounded-3xl text-center space-y-4 slide-up max-w-xs w-full">
    <h3 class="font-black text-lg">📱 QR ile Katıl</h3>
    <div id="qr-code" class="flex justify-center p-3 bg-white rounded-2xl"></div>
    <p class="font-mono font-black text-2xl accent-text" id="qr-room-code">----</p>
    <p class="text-xs opacity-40">Arkadaşların bu kodu tarasın</p>
    <button onclick="hide('modal-qr')" class="btn-secondary w-full py-2 text-sm">Kapat</button>
  </div>
</div>
<div id="modal-history" class="hidden fixed inset-0 z-[990] flex items-end justify-center p-4" style="background:rgba(0,0,0,.75);backdrop-filter:blur(10px)" onclick="if(event.target===this)hide('modal-history')">
  <div class="card w-full max-w-lg max-h-[80vh] overflow-y-auto no-scrollbar p-5 rounded-3xl space-y-4 slide-up">
    <div class="flex justify-between items-center">
      <h3 class="font-black text-lg">📖 Hikaye Geçmişi</h3>
      <button onclick="hide('modal-history')" class="w-8 h-8 rounded-full bg-white/10 text-lg flex items-center justify-center hover:bg-white/20">✕</button>
    </div>
    <div id="modal-history-list" class="space-y-3"></div>
    <p id="modal-history-empty" class="text-center opacity-40 text-sm py-6">Henüz tamamlanmış hikaye yok...</p>
  </div>
</div>



<div id="main-app" class="p-5 relative overflow-y-auto no-scrollbar">

<!-- ══ HEADER ══ -->
<div id="ui-header" class="mb-5">
  <div class="flex justify-between items-center gap-2">
    <div class="shrink-0">
      <span id="oda-label" class="hidden text-[8px] opacity-50 font-bold tracking-widest block">ODA</span>
      <span id="room-display" class="hidden accent-text font-mono text-xl font-black">----</span>
    </div>
    <div id="round-indicator" class="hidden text-center shrink-0">
      <span class="text-[8px] opacity-50 font-bold tracking-widest block">TUR</span>
      <span id="round-num" class="font-black text-lg">1/3</span>
    </div>
    <div class="flex items-center gap-2 ml-auto">
      <div class="text-right mr-1">
        <span id="mode-badge" class="text-[9px] bg-white/10 px-2 py-1 rounded-md uppercase font-bold tracking-wider block">Klasik</span>
        <div id="leader-badge" class="hidden text-[9px] text-yellow-400 font-bold mt-0.5">🏆 <span id="leader-name">-</span>: <span id="leader-score">0</span>p</div>
      </div>
      <!-- Controls: mute, history, theme -->
      <button id="btn-mute" onclick="toggleMute()" class="w-8 h-8 rounded-full card flex items-center justify-center text-sm shrink-0" title="Ses">🔊</button>
      <button id="btn-history" onclick="openHistory()" class="hidden w-8 h-8 rounded-full card flex items-center justify-center text-sm shrink-0" title="Hikayeler">📖</button>
      <div class="relative shrink-0">
        <button onclick="toggleThemePanel()" class="w-8 h-8 rounded-full card flex items-center justify-center text-base" title="UI Teması">🎨</button>
        <div id="theme-panel" class="hidden absolute right-0 top-10 card p-2 shadow-xl min-w-[160px] space-y-1 z-50">
          <p class="text-[8px] font-bold uppercase tracking-widest opacity-50 px-2 py-1">UI TEMASI</p>
          <button onclick="setUITheme('modern')"    class="theme-opt w-full text-left px-3 py-2 rounded-lg text-xs font-bold hover:bg-white/10">🌑 Modern</button>
          <button onclick="setUITheme('retro')"     class="theme-opt w-full text-left px-3 py-2 rounded-lg text-xs font-bold hover:bg-white/10">👾 Retro Pixel</button>
          <button onclick="setUITheme('newspaper')" class="theme-opt w-full text-left px-3 py-2 rounded-lg text-xs font-bold hover:bg-white/10">📰 Gazete</button>
          <button onclick="setUITheme('neon')"      class="theme-opt w-full text-left px-3 py-2 rounded-lg text-xs font-bold hover:bg-white/10">⚡ Neon Arcade</button>
          <button onclick="setUITheme('kawaii')"    class="theme-opt w-full text-left px-3 py-2 rounded-lg text-xs font-bold hover:bg-white/10">🌸 Kawaii</button>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ══ LOGIN ══ -->
<div id="screen-login" class="space-y-6 mt-6">
  <div class="text-center">
    <h1 class="text-5xl font-black font-['Unbounded'] tracking-tighter bg-gradient-to-br from-white to-slate-500 bg-clip-text text-transparent">KİM KİMLE?</h1>
    <p class="text-[10px] opacity-40 font-bold tracking-[.4em] uppercase mt-2">V8 Ultimate Edition</p>
  </div>
  <div class="flex justify-center">
    <div id="avatar-display" class="w-20 h-20 rounded-full card border-4 border-white/10 flex items-center justify-center text-4xl shadow-2xl pulse-ring">🐱</div>
  </div>
  <div class="flex justify-center gap-2 flex-wrap">
    <button onclick="setAvatar('🐱')" class="p-2 text-2xl card hover:bg-white/10 rounded-lg transition">🐱</button>
    <button onclick="setAvatar('👽')" class="p-2 text-2xl card hover:bg-white/10 rounded-lg transition">👽</button>
    <button onclick="setAvatar('👹')" class="p-2 text-2xl card hover:bg-white/10 rounded-lg transition">👹</button>
    <button onclick="setAvatar('🤖')" class="p-2 text-2xl card hover:bg-white/10 rounded-lg transition">🤖</button>
    <button onclick="setAvatar('🦊')" class="p-2 text-2xl card hover:bg-white/10 rounded-lg transition">🦊</button>
    <button onclick="setAvatar('🐸')" class="p-2 text-2xl card hover:bg-white/10 rounded-lg transition">🐸</button>
    <button onclick="setAvatar('🧙')" class="p-2 text-2xl card hover:bg-white/10 rounded-lg transition">🧙</button>
    <button onclick="setAvatar('👻')" class="p-2 text-2xl card hover:bg-white/10 rounded-lg transition">👻</button>
  </div>
  <div class="space-y-3">
    <input id="in-name" type="text" placeholder="İsmin" maxlength="20" class="input-field text-center" onkeydown="if(event.key==='Enter') createRoom()">
    <button onclick="createRoom()" class="btn-primary">ODA KUR</button>
    <div class="flex gap-2">
      <input id="in-room" type="text" placeholder="KOD" maxlength="4" class="input-field w-24 text-center font-mono uppercase" onkeydown="if(event.key==='Enter') joinRoom()">
      <button onclick="joinRoom()" class="btn-secondary flex-1">KATIL</button>
    </div>
    <button onclick="joinAsSpectator()" class="w-full text-center text-xs opacity-40 hover:opacity-70 transition py-1">👁️ İzleyici olarak katıl</button>
    <!-- Rejoin button (shown if session exists) -->
    <button id="btn-rejoin" class="hidden w-full text-center text-xs card rounded-xl py-3 px-4 font-bold opacity-70 hover:opacity-100 transition"></button>
  </div>
</div>

<!-- ══ LOBBY ══ -->
<div id="screen-lobby" class="hidden space-y-5">
  <div class="card p-4">
    <div class="flex justify-between items-center mb-3">
      <span class="text-xs font-bold opacity-50">OYUNCULAR</span>
      <div class="flex items-center gap-2">
        <span id="player-count" class="accent-text font-bold text-xs">1/7</span>
        <button onclick="showQR()" class="text-[10px] card px-2 py-1 rounded-lg font-bold opacity-60 hover:opacity-100 transition">📱 QR</button>
      </div>
    </div>
    <div id="list-players" class="space-y-2 max-h-[200px] overflow-y-auto no-scrollbar"></div>
    <p class="text-[9px] opacity-30 text-center mt-2 hidden" id="kick-hint">Host: uzun bas → oyuncuyu at</p>
  </div>

  <div id="host-controls" class="hidden space-y-4">
    <div class="card p-4 space-y-4" style="border-style:dashed;">
      <p class="text-[10px] opacity-40 font-black uppercase tracking-widest text-center">⚙️ HOST AYARLARI</p>
      <div class="grid grid-cols-2 gap-2">
        <button onclick="setMode('classic')"   id="btn-mode-classic"   class="mode-btn p-2 rounded-lg bg-indigo-600/20 text-indigo-300 border border-indigo-600/50 text-xs font-bold">Klasik</button>
        <button onclick="setMode('parallel')"  id="btn-mode-parallel"  class="mode-btn p-2 rounded-lg card text-xs font-bold">PARALEL 🌌</button>
        <button onclick="setMode('horror')"    id="btn-mode-horror"    class="mode-btn p-2 rounded-lg card text-xs font-bold">Korku 👻</button>
        <button onclick="setMode('scifi')"     id="btn-mode-scifi"     class="mode-btn p-2 rounded-lg card text-xs font-bold">Cyberpunk 🤖</button>
        <button onclick="setMode('absurd')"    id="btn-mode-absurd"    class="mode-btn p-2 rounded-lg card text-xs font-bold">Kaos 🌀</button>
        <button onclick="setMode('uzatilmis')" id="btn-mode-uzatilmis" class="mode-btn p-2 rounded-lg card text-xs font-bold">Uzatılmış ⏳</button>
        <button onclick="setMode('custom')"    id="btn-mode-custom"    class="mode-btn p-2 rounded-lg card text-xs font-bold col-span-2">Özel Sorular ✏️</button>
      </div>
      <div id="custom-editor" class="hidden space-y-2">
        <p class="text-[9px] opacity-50 font-bold uppercase tracking-widest">ÖZEL SORULAR (1 satır = 1 soru)</p>
        <textarea id="custom-questions-input" rows="6" placeholder="Kim?&#10;Nerede?&#10;Ne yaptı?&#10;Ne dedi?&#10;..." class="input-field text-sm" style="resize:vertical;font-size:.75rem;"></textarea>
        <p class="text-[9px] opacity-50 font-bold uppercase tracking-widest mt-2">HİKAYE ŞABLONU (isteğe bağlı)</p>
        <input id="custom-template-input" type="text" placeholder="{0} ile {1}, {2}'da {3} yaptı..." class="input-field text-sm">
        <button onclick="saveCustomQuestions()" class="btn-primary text-sm py-2">✓ KAYDET</button>
      </div>
      <div class="flex items-center justify-between card p-3">
        <span class="text-xs font-bold">👤 Yazarları Göster</span>
        <div class="relative inline-block w-10 align-middle select-none">
          <input type="checkbox" id="chk-names" class="toggle-checkbox absolute block w-5 h-5 rounded-full bg-white border-4 appearance-none cursor-pointer border-slate-700" onchange="toggleSetting('show_author',this.checked)"/>
          <label for="chk-names" class="toggle-label block overflow-hidden h-5 rounded-full bg-slate-700 cursor-pointer"></label>
        </div>
      </div>
    </div>
    <button onclick="startGame()" class="btn-primary text-lg shadow-xl">OYUNU BAŞLAT</button>
  </div>

  <div id="guest-waiting" class="hidden text-center py-6 space-y-3">
    <p class="opacity-50 text-sm">Host ayarları yapıyor...</p>
    <div class="flex flex-col items-center gap-2 text-xs opacity-40">
      <span class="card px-3 py-2">Mod: <span id="display-mode" class="accent-text font-bold">Klasik</span></span>
      <span id="display-visibility" class="card px-3 py-2">İsimler Gizli 🕵️</span>
    </div>
  </div>

  <div id="spectator-waiting" class="hidden text-center py-6">
    <p class="text-4xl mb-3">👁️</p>
    <p class="font-bold">İzleyici modunda bekleniyor...</p>
    <p class="text-xs opacity-40 mt-2">Oyun başladığında otomatik görürsün.</p>
  </div>
</div>

<!-- ══ CLASSIC GAME ══ -->
<div id="screen-game" class="hidden space-y-5">
  <div id="timer-wrap" class="hidden">
    <div class="flex justify-between items-center mb-1">
      <span class="text-[9px] opacity-40 font-bold uppercase tracking-wider">SÜRE</span>
      <span id="timer-num" class="font-black text-lg tabular-nums accent-text">20</span>
    </div>
    <div class="h-2 rounded-full overflow-hidden" style="background:rgba(255,255,255,.1)">
      <div id="timer-bar" class="h-full accent-bg rounded-full" style="width:100%"></div>
    </div>
  </div>
  <div id="turn-active" class="hidden text-center space-y-5">
    <div>
      <span id="step-badge" class="text-[9px] border border-white/20 px-2 py-1 rounded opacity-50">SORU 1</span>
      <h2 id="q-text" class="text-3xl font-black mt-2 font-['Unbounded']">SORU</h2>
    </div>
    <div id="paper" class="paper-sheet text-left shadow-2xl">
      <label class="text-[9px] font-bold text-slate-400 uppercase">CEVABIN:</label>
      <input type="text" id="in-ans" maxlength="80" autocomplete="off"
        class="w-full bg-slate-100 text-slate-900 text-lg font-bold p-2 mt-2 rounded border-b-2 border-slate-300 outline-none"
        oninput="onAnswerInput()"
        onkeydown="if(event.key==='Enter') submitAns()">
      <button onclick="submitAns()" class="accent-bg text-white w-full py-3 mt-4 rounded-xl font-bold">GÖNDER</button>
    </div>
  </div>
  <div id="turn-wait" class="hidden text-center py-8">
    <div id="wait-avatar" class="text-6xl animate-bounce mb-3"></div>
    <!-- Typing indicator -->
    <div id="typing-indicator" class="hidden flex justify-center gap-1 mb-2">
      <span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span>
    </div>
    <p id="wait-text" class="opacity-50 font-bold animate-pulse mb-5"></p>
    <!-- Host skip button -->
    <button id="btn-skip" onclick="skipQuestion()" class="hidden text-xs opacity-30 hover:opacity-80 transition card px-4 py-2 rounded-xl font-bold mb-5">⏭ Soruyu Atla</button>
    <!-- Quick phrases -->
    <div class="space-y-2">
      <p class="text-[9px] opacity-30 uppercase tracking-widest font-bold">Hızlı Tepki</p>
      <div class="flex gap-2 flex-wrap justify-center">
        <button class="phrase-btn" onclick="sendPhrase('😭 Bu çok iyi!')">😭 Bu çok iyi!</button>
        <button class="phrase-btn" onclick="sendPhrase('🤔 Zor soru...')">🤔 Zor soru...</button>
        <button class="phrase-btn" onclick="sendPhrase('😂 Kahkaha!')">😂 Kahkaha!</button>
        <button class="phrase-btn" onclick="sendPhrase('🔥 Harika!')">🔥 Harika!</button>
        <button class="phrase-btn" onclick="sendPhrase('💀 Öldüm!')">💀 Öldüm!</button>
        <button class="phrase-btn" onclick="sendPhrase('👀 Merak ettim')">👀 Merak ettim</button>
      </div>
    </div>
  </div>
  <!-- Skipped notification -->
  <div id="skip-toast" class="hidden text-center text-xs opacity-60 py-1 animate-pulse"></div>
</div>

<!-- ══ SPECTATOR GAME VIEW ══ -->
<div id="screen-spectator" class="hidden text-center py-14 space-y-4">
  <p class="spec-badge inline-block mb-2">👁️ İZLEYİCİ</p>
  <div id="spec-avatar" class="text-6xl animate-pulse"></div>
  <p id="spec-text" class="font-bold opacity-60"></p>
  <p id="spec-question" class="text-2xl font-black accent-text mt-2"></p>
  <!-- Spectator emoji reactions -->
  <div class="flex justify-center gap-3 mt-4">
    <button onclick="sendEmoji('😂')" class="text-2xl card p-2 rounded-xl hover:bg-white/15 hover:scale-110 transition active:scale-90">😂</button>
    <button onclick="sendEmoji('👻')" class="text-2xl card p-2 rounded-xl hover:bg-white/15 hover:scale-110 transition active:scale-90">👻</button>
    <button onclick="sendEmoji('🔥')" class="text-2xl card p-2 rounded-xl hover:bg-white/15 hover:scale-110 transition active:scale-90">🔥</button>
    <button onclick="sendEmoji('💀')" class="text-2xl card p-2 rounded-xl hover:bg-white/15 hover:scale-110 transition active:scale-90">💀</button>
  </div>
  <p class="text-[9px] opacity-30 mt-1">Reaksiyonların 👁️ ile görünür</p>
</div>

<!-- ══ PARALLEL GAME ══ -->
<div id="screen-parallel" class="hidden space-y-5">
  <div id="p-timer-wrap" class="hidden">
    <div class="flex justify-between items-center mb-1">
      <span class="text-[9px] opacity-40 font-bold uppercase tracking-wider">SÜRE</span>
      <span id="p-timer-num" class="font-black text-lg tabular-nums" style="color:#10b981">20</span>
    </div>
    <div class="h-2 rounded-full overflow-hidden" style="background:rgba(255,255,255,.1)">
      <div id="p-timer-bar" class="h-full rounded-full" style="width:100%;background:#10b981"></div>
    </div>
  </div>
  <div id="p-answering" class="hidden text-center space-y-5">
    <div>
      <span id="p-step-badge" class="text-[9px] border px-2 py-1 rounded opacity-50" style="border-color:#10b981;color:#10b981">SORU 1</span>
      <h2 id="p-q-text" class="text-3xl font-black mt-2 font-['Unbounded']" style="color:#10b981">SORU</h2>
    </div>
    <div class="card p-5">
      <input type="text" id="in-p-ans" maxlength="80" autocomplete="off"
        class="input-field text-lg mb-4" oninput="onAnswerInput()" onkeydown="if(event.key==='Enter') submitParallelAns()">
      <button onclick="submitParallelAns()" class="btn-primary" style="background:#10b981">GÖNDER</button>
    </div>
  </div>
  <div id="p-waiting" class="hidden text-center py-10">
    <p class="text-5xl mb-4">⏳</p>
    <p class="opacity-50 font-bold animate-pulse">Cevabın gönderildi!</p>
    <p id="p-answer-count" class="text-sm font-bold mt-3 accent-text">0/? cevapladı</p>
    <div id="p-answer-avatars" class="flex justify-center gap-1 mt-3 flex-wrap"></div>
    <div class="mt-6 space-y-2">
      <p class="text-[9px] opacity-30 uppercase tracking-widest font-bold">Hızlı Tepki</p>
      <div class="flex gap-2 flex-wrap justify-center">
        <button class="phrase-btn" onclick="sendPhrase('😭 Bu çok iyi!')">😭 Bu çok iyi!</button>
        <button class="phrase-btn" onclick="sendPhrase('🤔 Hmm...')">🤔 Hmm...</button>
        <button class="phrase-btn" onclick="sendPhrase('🔥 Harika!')">🔥 Harika!</button>
        <button class="phrase-btn" onclick="sendPhrase('💀 Öldüm!')">💀 Öldüm!</button>
      </div>
    </div>
  </div>
  <div id="p-voting" class="hidden space-y-4">
    <div class="text-center">
      <h3 class="text-xl font-bold">OYLAMA</h3>
      <p id="vote-info" class="text-xs opacity-50 mt-1">En iyi cevabı seç!</p>
    </div>
    <div id="vote-list" class="space-y-2 max-h-[55vh] overflow-y-auto pb-4 no-scrollbar"></div>
  </div>
</div>

<!-- ══ STORY REVEAL ══ -->
<div id="screen-reveal" class="hidden space-y-4">
  <div class="text-center">
    <span id="reveal-round-badge" class="text-[9px] accent-bg text-white px-3 py-1.5 rounded-full font-bold tracking-wider">TUR 1/3</span>
  </div>
  <div class="card p-6 rounded-[2rem] shadow-2xl min-h-[140px] flex items-center justify-center">
    <p id="reveal-text" class="text-lg font-medium leading-relaxed font-serif text-center"></p>
  </div>
  <!-- Per-answer animated reveal -->
  <div id="answer-reveal-list" class="hidden space-y-2"></div>
  <div id="reveal-reactions" class="hidden text-center space-y-2">
    <p class="text-[9px] opacity-40 uppercase font-bold tracking-widest">TEPKİ VER</p>
    <div class="flex justify-center gap-3">
      <button onclick="sendEmoji('😂')" class="text-3xl card p-3 rounded-2xl hover:bg-white/15 hover:scale-110 transition active:scale-90">😂</button>
      <button onclick="sendEmoji('💀')" class="text-3xl card p-3 rounded-2xl hover:bg-white/15 hover:scale-110 transition active:scale-90">💀</button>
      <button onclick="sendEmoji('🔥')" class="text-3xl card p-3 rounded-2xl hover:bg-white/15 hover:scale-110 transition active:scale-90">🔥</button>
      <button onclick="sendEmoji('🤡')" class="text-3xl card p-3 rounded-2xl hover:bg-white/15 hover:scale-110 transition active:scale-90">🤡</button>
      <button onclick="sendEmoji('👑')" class="text-3xl card p-3 rounded-2xl hover:bg-white/15 hover:scale-110 transition active:scale-90">👑</button>
    </div>
  </div>
  <!-- Vote with categories -->
  <div id="reveal-vote" class="hidden space-y-2">
    <p class="text-[9px] text-center opacity-40 uppercase font-bold tracking-widest">EN İYİ CEVAP? (+1 PUAN)</p>
    <div class="flex justify-center gap-2 mb-2">
      <span class="text-[9px] px-2 py-1 rounded-full border border-white/20 opacity-60">😂 Komik</span>
      <span class="text-[9px] px-2 py-1 rounded-full border border-white/20 opacity-60">💡 Yaratıcı</span>
      <span class="text-[9px] px-2 py-1 rounded-full border border-white/20 opacity-60">😱 Şok</span>
    </div>
    <div id="answer-vote-list" class="space-y-2 max-h-[30vh] overflow-y-auto no-scrollbar"></div>
    <p id="vote-submitted-msg" class="hidden text-center text-xs opacity-40 py-2 animate-pulse">✓ Oy verildi, bekleniyor...</p>
  </div>
  <div id="reveal-result" class="hidden space-y-3">
    <div class="card p-4 rounded-2xl text-center" style="background:rgba(234,179,8,.08);border-color:rgba(234,179,8,.3)">
      <p id="winner-text" class="text-yellow-400 font-black"></p>
      <p id="winner-category" class="text-yellow-500 text-xs mt-1 font-bold opacity-70"></p>
    </div>
    <button id="btn-next-round"    onclick="triggerNextRound()" class="hidden btn-primary text-lg">SONRAKI TUR →</button>
    <button id="btn-final-results" onclick="triggerNextRound()" class="hidden w-full py-4 rounded-xl font-black text-lg text-white" style="background:linear-gradient(135deg,#f59e0b,#ef4444)">🏆 SONUÇLARI GÖR</button>
    <p id="waiting-next" class="hidden text-center opacity-40 text-sm animate-pulse">Host sıradaki turu başlatıyor...</p>
  </div>
</div>

<!-- ══ BETWEEN-ROUNDS SCORES ══ -->
<div id="screen-scores" class="hidden space-y-5">
  <div class="text-center">
    <p class="text-[9px] opacity-40 uppercase tracking-widest font-bold">PUAN TABLOSU</p>
    <p id="scores-subtitle" class="font-bold mt-1"></p>
  </div>
  <div id="scores-list" class="space-y-3"></div>

  <!-- Mid-game mode switch (host only) -->
  <div id="scores-mode-switch" class="hidden card p-3 space-y-2">
    <p class="text-[9px] opacity-40 uppercase tracking-widest font-bold text-center">🎮 SONRAKI TUR MODU DEĞİŞTİR</p>
    <div class="grid grid-cols-3 gap-1.5">
      <button onclick="selectMidMode('classic')"   class="mid-mode-btn p-2 rounded-lg card text-[9px] font-bold hover:bg-white/10">Klasik</button>
      <button onclick="selectMidMode('parallel')"  class="mid-mode-btn p-2 rounded-lg card text-[9px] font-bold hover:bg-white/10">Paralel 🌌</button>
      <button onclick="selectMidMode('horror')"    class="mid-mode-btn p-2 rounded-lg card text-[9px] font-bold hover:bg-white/10">Korku 👻</button>
      <button onclick="selectMidMode('scifi')"     class="mid-mode-btn p-2 rounded-lg card text-[9px] font-bold hover:bg-white/10">Cyberpunk 🤖</button>
      <button onclick="selectMidMode('absurd')"    class="mid-mode-btn p-2 rounded-lg card text-[9px] font-bold hover:bg-white/10">Kaos 🌀</button>
      <button onclick="selectMidMode('uzatilmis')" class="mid-mode-btn p-2 rounded-lg card text-[9px] font-bold hover:bg-white/10">Uzatılmış ⏳</button>
    </div>
    <p id="mid-mode-selected" class="text-[9px] text-center accent-text font-bold hidden"></p>
  </div>

  <div id="scores-host-btn" class="hidden">
    <button onclick="confirmNextRound()" class="btn-primary text-lg mt-2">TUR <span id="next-round-num">2</span>'Yİ BAŞLAT ▶</button>
  </div>
  <p id="scores-guest-wait" class="hidden text-center opacity-40 text-sm animate-pulse">Host sıradaki turu başlatıyor...</p>
</div>

<!-- ══ STORY BOOK (final) ══ -->
<div id="screen-final" class="hidden space-y-5">
  <div class="text-center">
    <p class="text-[9px] opacity-40 uppercase tracking-widest font-bold mb-3">🎉 OYUN BİTTİ</p>
    <div class="card p-6 rounded-3xl" style="background:rgba(234,179,8,.1);border-color:rgba(234,179,8,.3)">
      <div id="champion-avatar" class="text-6xl mb-2">🏆</div>
      <p id="champion-name" class="text-yellow-400 font-black text-2xl"></p>
      <p id="champion-score" class="text-yellow-500 opacity-70 text-sm font-bold mt-1"></p>
    </div>
  </div>
  <div id="final-scores" class="space-y-2"></div>

  <div>
    <p class="text-[9px] opacity-40 uppercase tracking-widest font-bold mb-3 text-center">📖 HİKAYE KİTABI</p>
    <div id="story-book" class="space-y-4"></div>
  </div>

  <div class="grid grid-cols-2 gap-2 mt-2">
    <button onclick="replayStories()" class="p-3 rounded-xl font-bold text-sm card hover:bg-white/10 transition">▶ Tekrar Oynat</button>
    <button onclick="shareWhatsapp()" class="p-3 rounded-xl flex items-center justify-center gap-2 font-bold text-sm text-white" style="background:#25D366">
      <svg class="w-5 h-5" fill="currentColor" viewBox="0 0 24 24"><path d="M.057 24l1.687-6.163c-1.041-1.804-1.588-3.849-1.587-5.946.003-6.556 5.338-11.891 11.893-11.891 3.181.001 6.167 1.24 8.413 3.488 2.245 2.248 3.481 5.236 3.48 8.414-.003 6.557-5.338 11.892-11.893 11.892-1.99-.001-3.951-.5-5.688-1.448l-6.305 1.654zm6.597-3.807c1.676.995 3.276 1.591 5.392 1.592 5.448 0 9.886-4.434 9.889-9.885.002-5.462-4.415-9.89-9.881-9.892-5.452 0-9.887 4.434-9.889 9.884-.001 2.225.651 3.891 1.746 5.634l-.999 3.648 3.742-.981zm11.387-5.464c-.074-.124-.272-.198-.57-.347-.297-.149-1.758-.868-2.031-.967-.272-.099-.47-.149-.669.149-.198.297-.768.967-.941 1.165-.173.198-.347.223-.644.074-.297-.149-1.255-.462-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.297-.347.446-.521.151-.172.2-.296.3-.495.099-.198.05-.372-.025-.521-.075-.148-.669-1.611-.916-2.206-.242-.579-.487-.501-.669-.51l-.57-.01c-.198 0-.52.074-.792.372-.272.297-1.04 1.017-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.095 3.2 5.076 4.487.709.306 1.263.489 1.694.626.712.226 1.36.194 1.872.118.571-.085 1.758-.719 2.006-1.413.248-.695.248-1.29.173-1.414z"/></svg>
      WhatsApp
    </button>
    <button onclick="shareTwitter()" class="p-3 rounded-xl flex items-center justify-center gap-2 font-bold text-sm text-white" style="background:#1DA1F2">
      <svg class="w-5 h-5" fill="currentColor" viewBox="0 0 24 24"><path d="M24 4.557c-.883.392-1.832.656-2.828.775 1.017-.609 1.798-1.574 2.165-2.724-.951.564-2.005.974-3.127 1.195-.897-.957-2.178-1.555-3.594-1.555-3.179 0-5.515 2.966-4.797 6.045-4.091-.205-7.719-2.165-10.148-5.144-1.29 2.213-.669 5.108 1.523 6.574-.806-.026-1.566-.247-2.229-.616-.054 2.281 1.581 4.415 3.949 4.89-.693.188-1.452.232-2.224.084.626 1.956 2.444 3.379 4.6 3.419-2.07 1.623-4.678 2.348-7.29 2.04 2.179 1.397 4.768 2.212 7.548 2.212 9.142 0 14.307-7.721 13.995-14.646.962-.695 1.797-1.562 2.457-2.549z"/></svg>
      Twitter
    </button>
    <button onclick="location.reload()" class="btn-secondary p-3 rounded-xl text-sm font-bold col-span-2">YENİ OYUN</button>
  </div>
</div>

</div><!-- /main-app -->

<script>
const socket = io();
let myAvatar = '🐱', myRoom = '', isHost = false, isSpectator = false, currentMode = 'classic';
let playerName = '';
let totalRounds = 3, currentRound = 1, currentQCount = 7;
let currentAttributedAnswers = [], hasVotedAnswer = false, lastStory = '', allStories = [];
let completedStories = [];
let selectedMidMode = null;
const ct = {id:null}, pt = {id:null};
let soundMuted = false;
let audioCtx = null;

// ── utils ──────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const hide = id => { const e=$(id); if(e) e.classList.add('hidden'); };
const show = id => { const e=$(id); if(e) e.classList.remove('hidden'); };
const SCREENS = ['screen-login','screen-lobby','screen-game','screen-parallel',
                 'screen-reveal','screen-scores','screen-final','screen-spectator'];
function showScreen(s) {
  SCREENS.forEach(x => { const e=$(x); if(e) e.classList.add('hidden'); });
  const t=$(s); if(t) t.classList.remove('hidden');
}

// ── SOUND ENGINE ───────────────────────────────────────────────────────────
function getAudioCtx() {
  if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  return audioCtx;
}
function playSound(type) {
  if (soundMuted) return;
  try {
    const ctx = getAudioCtx();
    if (type === 'tick') {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.connect(g); g.connect(ctx.destination);
      o.frequency.value = 880;
      g.gain.setValueAtTime(0.07, ctx.currentTime);
      g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.08);
      o.start(ctx.currentTime); o.stop(ctx.currentTime + 0.08);
    } else if (type === 'whoosh') {
      const buf = ctx.createBuffer(1, Math.floor(ctx.sampleRate * 0.5), ctx.sampleRate);
      const data = buf.getChannelData(0);
      for (let i = 0; i < data.length; i++) data[i] = (Math.random()*2-1) * (1 - i/data.length);
      const src = ctx.createBufferSource(); src.buffer = buf;
      const flt = ctx.createBiquadFilter(); flt.type = 'bandpass'; flt.frequency.value = 900;
      const g = ctx.createGain();
      g.gain.setValueAtTime(0.25, ctx.currentTime);
      g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.5);
      src.connect(flt); flt.connect(g); g.connect(ctx.destination);
      src.start(ctx.currentTime);
    } else if (type === 'pop') {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.connect(g); g.connect(ctx.destination);
      o.type = 'sine';
      o.frequency.setValueAtTime(500, ctx.currentTime);
      o.frequency.exponentialRampToValueAtTime(120, ctx.currentTime + 0.12);
      g.gain.setValueAtTime(0.25, ctx.currentTime);
      g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.15);
      o.start(ctx.currentTime); o.stop(ctx.currentTime + 0.15);
    } else if (type === 'win') {
      [523, 659, 784, 1047].forEach((freq, i) => {
        const o = ctx.createOscillator(), g = ctx.createGain();
        o.connect(g); g.connect(ctx.destination);
        o.frequency.value = freq; o.type = 'sine';
        const t = ctx.currentTime + i * 0.13;
        g.gain.setValueAtTime(0.18, t);
        g.gain.exponentialRampToValueAtTime(0.001, t + 0.28);
        o.start(t); o.stop(t + 0.28);
      });
    } else if (type === 'skip') {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.connect(g); g.connect(ctx.destination);
      o.type = 'square'; o.frequency.value = 300;
      g.gain.setValueAtTime(0.1, ctx.currentTime);
      g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.18);
      o.start(ctx.currentTime); o.stop(ctx.currentTime + 0.18);
    }
  } catch(e) {}
}
function toggleMute() {
  soundMuted = !soundMuted;
  $('btn-mute').textContent = soundMuted ? '🔇' : '🔊';
  if (!soundMuted) playSound('pop');
}

// ── UI THEMES ──────────────────────────────────────────────────────────────
function setUITheme(t) {
  document.body.classList.forEach(c => { if(c.startsWith('ui-')) document.body.classList.remove(c); });
  document.body.classList.add('ui-'+t);
  document.querySelectorAll('.theme-opt').forEach(b => b.classList.remove('accent-bg','text-white'));
  const active = document.querySelector(`.theme-opt[onclick="setUITheme('${t}')"]`);
  if (active) active.classList.add('accent-bg','text-white');
  hide('theme-panel');
  playSound('pop');
}
function toggleThemePanel() { $('theme-panel').classList.toggle('hidden'); }
document.addEventListener('click', e => {
  if (!e.target.closest('#theme-panel') && !e.target.closest('[onclick="toggleThemePanel()"]')) hide('theme-panel');
}, {passive:true});

const MODE_GAME_THEME = {classic:'game-indigo',horror:'game-red',scifi:'game-cyan',parallel:'game-parallel',absurd:'game-purple',uzatilmis:'game-orange',custom:'game-indigo'};
function applyGameTheme(mode) {
  Object.values(MODE_GAME_THEME).forEach(t => document.body.classList.remove(t));
  document.body.classList.add(MODE_GAME_THEME[mode] || 'game-indigo');
}

// ── avatars ────────────────────────────────────────────────────────────────
function setAvatar(c) { myAvatar = c; $('avatar-display').innerText = c; playSound('pop'); }

// ── room persistence ───────────────────────────────────────────────────────
function saveSession() {
  try {
    sessionStorage.setItem('kkv8', JSON.stringify({room:myRoom,name:playerName,avatar:myAvatar,sid:socket.id}));
  } catch(e) {}
}
(function loadSession() {
  try {
    const saved = JSON.parse(sessionStorage.getItem('kkv8'));
    if (saved && saved.room) {
      const btn = $('btn-rejoin');
      btn.innerHTML = `🔄 <strong>${saved.room}</strong> odasına geri dön ${saved.avatar} ${saved.name}`;
      btn.classList.remove('hidden');
      btn.onclick = () => {
        myAvatar = saved.avatar; playerName = saved.name;
        socket.emit('rejoin_room', {room:saved.room,name:saved.name,avatar:saved.avatar,old_sid:saved.sid});
      };
    }
  } catch(e) {}
})();

// ── room ───────────────────────────────────────────────────────────────────
function createRoom() {
  const n = $('in-name').value.trim();
  if (!n) return alert('İsim yaz!');
  playerName = n; isHost = true; isSpectator = false;
  socket.emit('create_room', {name:n, avatar:myAvatar});
}
function joinRoom() {
  const n = $('in-name').value.trim(), r = $('in-room').value.toUpperCase().trim();
  if (!n||!r) return alert('Eksik bilgi!');
  playerName = n; isSpectator = false;
  socket.emit('join_room', {name:n, room:r, avatar:myAvatar, spectator:false});
}
function joinAsSpectator() {
  const n = $('in-name').value.trim() || 'İzleyici';
  const r = $('in-room').value.toUpperCase().trim();
  if (!r) return alert('Oda kodu gir!');
  playerName = n; isSpectator = true;
  socket.emit('join_room', {name:n, room:r, avatar:'👁️', spectator:true});
}

// ── settings ───────────────────────────────────────────────────────────────
function setMode(m) {
  currentMode = m;
  document.querySelectorAll('.mode-btn').forEach(b => { b.className = 'mode-btn p-2 rounded-lg card text-xs font-bold'; });
  const active = $('btn-mode-'+m);
  if (active) active.className = 'mode-btn p-2 rounded-lg bg-indigo-600/20 text-indigo-300 border border-indigo-600/50 text-xs font-bold';
  m === 'custom' ? show('custom-editor') : hide('custom-editor');
  if (m !== 'custom') socket.emit('update_settings', {room:myRoom, key:'mode', val:m});
  applyGameTheme(m);
}
function toggleSetting(k,v) { socket.emit('update_settings', {room:myRoom, key:k, val:v}); }
function saveCustomQuestions() {
  const raw = $('custom-questions-input').value.trim();
  const tmpl = $('custom-template-input').value.trim();
  const qs = raw.split('\n').map(s=>s.trim()).filter(Boolean);
  if (qs.length < 3) return alert('En az 3 soru gir!');
  socket.emit('update_settings', {room:myRoom, key:'mode', val:'custom'});
  socket.emit('update_settings', {room:myRoom, key:'custom_questions', val:qs, template:tmpl});
  alert(`${qs.length} soru kaydedildi ✓`);
}
function startGame() { socket.emit('start_game', {room:myRoom}); }

// ── mid-game mode switch ───────────────────────────────────────────────────
function selectMidMode(m) {
  selectedMidMode = m;
  document.querySelectorAll('.mid-mode-btn').forEach(b => b.classList.remove('accent-bg','text-white'));
  event.target.classList.add('accent-bg','text-white');
  $('mid-mode-selected').innerText = `✓ Sonraki tur: ${m}`;
  show('mid-mode-selected');
  playSound('pop');
}

// ── skip question ──────────────────────────────────────────────────────────
function skipQuestion() {
  playSound('skip');
  socket.emit('skip_question', {room:myRoom});
}

// ── timers ─────────────────────────────────────────────────────────────────
function runTimer(secs, numId, barId, ref, onExpire) {
  clearInterval(ref.id);
  let s = secs;
  const num=$(numId), bar=$(barId);
  if (num) num.innerText = s;
  if (bar) { bar.style.width = '100%'; bar.style.backgroundColor = ''; }
  ref.id = setInterval(() => {
    s--;
    if (num) num.innerText = s;
    if (bar) { bar.style.width = (s/secs*100)+'%'; if(s<=5) bar.style.backgroundColor='#ef4444'; }
    if (s > 0 && s <= 5) playSound('tick');
    if (s <= 0) { clearInterval(ref.id); onExpire(); }
  }, 1000);
}
function startClassicTimer(secs) { show('timer-wrap'); runTimer(secs,'timer-num','timer-bar',ct,()=>submitAns()); }
function stopClassicTimer() { clearInterval(ct.id); hide('timer-wrap'); }
function startParallelTimer(secs) { show('p-timer-wrap'); runTimer(secs,'p-timer-num','p-timer-bar',pt,()=>submitParallelAns()); }
function stopParallelTimer() { clearInterval(pt.id); hide('p-timer-wrap'); }

// ── classic ─────────────────────────────────────────────────────────────────
function submitAns() {
  const ans = $('in-ans').value.trim() || '...';
  stopClassicTimer();
  $('paper').classList.add('paper-fly');
  setTimeout(() => { socket.emit('submit_ans',{room:myRoom,ans}); $('in-ans').value=''; $('paper').classList.remove('paper-fly'); }, 600);
}

// ── parallel ────────────────────────────────────────────────────────────────
function submitParallelAns() {
  const ans = $('in-p-ans').value.trim() || '...';
  stopParallelTimer(); hide('p-answering'); show('p-waiting');
  socket.emit('submit_parallel_ans', {room:myRoom, ans}); $('in-p-ans').value='';
}
function castVote(cid) { hide('p-voting'); show('p-waiting'); socket.emit('cast_vote',{room:myRoom,candidate_id:cid}); }

// ── emoji ───────────────────────────────────────────────────────────────────
function sendEmoji(e) { socket.emit('send_emoji', {room:myRoom, emoji:e, is_spectator:isSpectator}); playSound('pop'); }
function spawnEmoji(e, isSpec) {
  const el = document.createElement('div');
  el.className = 'float-emoji' + (isSpec ? ' spec' : '');
  el.style.left = (Math.random()*75+10)+'%';
  el.style.bottom = '15%';
  if (isSpec) {
    el.innerHTML = `<span style="position:relative;display:inline-block">${e}<span style="font-size:.45em;position:absolute;bottom:-2px;right:-6px">👁️</span></span>`;
  } else {
    el.textContent = e;
  }
  document.body.appendChild(el);
  setTimeout(()=>el.remove(), 2100);
}

function revealStory(story, answers) {
  playSound('whoosh');
  const el = $('reveal-text'); el.innerHTML = '';
  hide('answer-reveal-list');
  const words = story.split(' ');
  const delay = Math.max(60, Math.min(180, 1800/words.length));
  words.forEach((w,i) => {
    const sp = document.createElement('span');
    sp.className = 'story-word'; sp.textContent = w+' ';
    el.appendChild(sp);
    setTimeout(()=>sp.classList.add('vis'), 300+i*delay);
  });
  const totalDelay = 300 + words.length*delay + 700;
  setTimeout(()=>{ show('reveal-reactions'); show('reveal-vote'); renderAnswerVotes(); }, totalDelay);
  setTimeout(()=>revealAnswers(answers), totalDelay + 400);
}

// ── vote with categories ────────────────────────────────────────────────────
function renderAnswerVotes() {
  const list = $('answer-vote-list'); list.innerHTML = ''; hasVotedAnswer = false;
  const cats = [{emoji:'😂',label:'Komik'},{emoji:'💡',label:'Yaratıcı'},{emoji:'😱',label:'Şok'}];
  currentAttributedAnswers.forEach(a => {
    const mine = a.owner_id === socket.id;
    const div = document.createElement('div');
    div.className = 'card p-3 rounded-xl space-y-2' + (mine ? ' opacity-50' : '');
    let catBtns = '';
    if (!mine) {
      catBtns = `<div class="flex gap-1.5">${cats.map(c => `
        <button onclick="submitAnswerVote('${a.owner_id}','${c.emoji}')"
          class="flex-1 py-1.5 rounded-lg text-[10px] font-bold border border-white/10 hover:bg-white/15 transition avote-cat-btn"
          data-owner="${a.owner_id}">${c.emoji} ${c.label}</button>`).join('')}</div>`;
    }
    div.innerHTML = `
      <div class="flex items-center gap-2">
        <span class="text-xl shrink-0">${a.owner_avatar}</span>
        <div class="flex-1 min-w-0">
          <div class="font-bold text-sm truncate">"${a.text}"</div>
          <div class="text-[10px] opacity-40 mt-0.5">${a.owner_name} · ${a.question}</div>
        </div>
        ${mine?'<span class="text-[9px] opacity-30 uppercase shrink-0">SEN</span>':''}
      </div>${catBtns}`;
    list.appendChild(div);
  });
}
function submitAnswerVote(ownerId, category) {
  if (hasVotedAnswer) return; hasVotedAnswer = true;
  document.querySelectorAll('.avote-cat-btn').forEach(b => { b.disabled=true; b.classList.add('opacity-20'); });
  // highlight selected
  document.querySelectorAll(`.avote-cat-btn[data-owner="${ownerId}"]`).forEach(b => {
    if (b.textContent.startsWith(category)) { b.classList.remove('opacity-20'); b.classList.add('ring-1','ring-yellow-400'); }
  });
  hide('reveal-vote'); show('vote-submitted-msg');
  socket.emit('submit_answer_vote', {room:myRoom, voted_for:ownerId, category});
}

// ── round history ───────────────────────────────────────────────────────────
function openHistory() {
  const list = $('modal-history-list');
  const empty = $('modal-history-empty');
  if (completedStories.length === 0) {
    list.innerHTML = ''; show('modal-history-empty');
  } else {
    hide('modal-history-empty');
    list.innerHTML = completedStories.map((s,i) => `
      <div class="card p-4 rounded-2xl space-y-2">
        <div class="flex items-center gap-2">
          <span class="text-[9px] accent-bg text-white px-2 py-0.5 rounded font-bold">TUR ${s.round}</span>
        </div>
        <p class="font-serif text-sm leading-relaxed">"${s.story}"</p>
        <div class="space-y-1 border-t pt-2" style="border-color:rgba(255,255,255,.08)">
          ${s.attributed_answers.map(a=>`
            <div class="flex items-center gap-1 text-[10px]">
              <span class="opacity-40 w-20 shrink-0 truncate">${a.question}</span>
              <span class="opacity-30">→</span>
              <span class="font-bold truncate">"${a.text}"</span>
              <span class="ml-auto opacity-30 shrink-0">${a.owner_avatar}</span>
            </div>`).join('')}
        </div>
      </div>`).join('');
  }
  show('modal-history');
}

// ── post-game replay ────────────────────────────────────────────────────────
function replayStories() {
  if (!allStories.length) return;
  let idx = 0;
  function showNext() {
    if (idx >= allStories.length) return;
    const st = allStories[idx]; const isLast = idx === allStories.length-1;
    const overlay = document.createElement('div');
    overlay.className = 'fixed inset-0 z-[995] flex items-center justify-center p-6';
    overlay.style.cssText = 'background:rgba(0,0,0,.92);backdrop-filter:blur(16px)';
    overlay.innerHTML = `
      <div class="text-center space-y-5 max-w-sm w-full">
        <span class="text-[9px] accent-bg text-white px-3 py-1 rounded-full font-bold">TUR ${idx+1} / ${allStories.length}</span>
        <p id="rp-text-${idx}" class="text-lg font-serif leading-relaxed mt-4 min-h-[80px]"></p>
        <button id="rp-btn-${idx}" class="btn-primary py-2 opacity-0 transition-opacity duration-700 pointer-events-none">
          ${isLast ? '✓ Tamam' : 'Sonraki Hikaye →'}
        </button>
      </div>`;
    document.body.appendChild(overlay);
    const textEl = overlay.querySelector(`#rp-text-${idx}`);
    const words = st.story.split(' ');
    const delay = Math.max(60, Math.min(140, 1400/words.length));
    words.forEach((w,wi) => {
      const sp = document.createElement('span');
      sp.className = 'story-word'; sp.textContent = w+' ';
      textEl.appendChild(sp);
      setTimeout(()=>sp.classList.add('vis'), 150+wi*delay);
    });
    playSound('whoosh');
    setTimeout(() => {
      const btn = overlay.querySelector(`#rp-btn-${idx}`);
      if (btn) { btn.style.opacity='1'; btn.classList.remove('pointer-events-none'); }
    }, 150+words.length*delay+400);
    overlay.querySelector(`#rp-btn-${idx}`).onclick = () => {
      overlay.remove(); idx++;
      if (idx < allStories.length) showNext();
    };
  }
  showNext();
}

// ── QR code ────────────────────────────────────────────────────────────────
let qrInstance = null;
function showQR() {
  const code = myRoom; if (!code) return;
  $('qr-room-code').innerText = code;
  const container = $('qr-code'); container.innerHTML = '';
  const url = window.location.origin + '?room=' + code;
  qrInstance = new QRCode(container, {text: url, width: 180, height: 180,
    colorDark:'#000000', colorLight:'#ffffff', correctLevel: QRCode.CorrectLevel.M});
  show('modal-qr');
}

// ── kick player ─────────────────────────────────────────────────────────────
let kickPressTimer = null;
function kickPlayer(id, name) {
  if (!isHost) return;
  if (confirm(`"${name}" adlı oyuncuyu atmak istiyor musun?`)) {
    socket.emit('kick_player', {room: myRoom, target_id: id});
    playSound('skip');
  }
}
function startKickPress(id, name) {
  kickPressTimer = setTimeout(() => kickPlayer(id, name), 700);
}
function cancelKickPress() {
  if (kickPressTimer) { clearTimeout(kickPressTimer); kickPressTimer = null; }
}

// ── typing indicator ────────────────────────────────────────────────────────
let typingTimeout = null;
let isTyping = false;
function onAnswerInput() {
  if (!isTyping) {
    isTyping = true;
    socket.emit('typing', {room: myRoom, is_typing: true});
  }
  clearTimeout(typingTimeout);
  typingTimeout = setTimeout(() => {
    isTyping = false;
    socket.emit('typing', {room: myRoom, is_typing: false});
  }, 1500);
}

// ── quick phrases ───────────────────────────────────────────────────────────
function sendPhrase(text) {
  socket.emit('send_phrase', {room: myRoom, text, name: playerName, avatar: myAvatar});
  playSound('pop');
  spawnPhraseToast(text, myAvatar, true);
}
function spawnPhraseToast(text, avatar, isSelf) {
  const el = document.createElement('div');
  el.className = 'phrase-toast';
  el.textContent = avatar + ' ' + text;
  el.style.left = (isSelf ? 30 : Math.random()*55+10) + '%';
  el.style.bottom = (isSelf ? '25%' : (Math.random()*20+8) + '%');
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2400);
}

// ── per-answer animated reveal ──────────────────────────────────────────────
function revealAnswers(answers) {
  const list = $('answer-reveal-list');
  list.innerHTML = ''; show('answer-reveal-list');
  answers.forEach((a, i) => {
    const el = document.createElement('div');
    el.className = 'answer-reveal flex items-center gap-3 card p-2.5 rounded-xl';
    el.style.animationDelay = (i * 0.18) + 's';
    el.innerHTML = `
      <span class="text-xl shrink-0">${a.owner_avatar}</span>
      <div class="flex-1 min-w-0">
        <div class="text-[9px] opacity-40 font-bold uppercase tracking-wider">${a.question}</div>
        <div class="font-bold text-sm truncate">"${a.text}"</div>
      </div>
      <span class="text-[9px] opacity-40 shrink-0 font-bold">${a.owner_name}</span>`;
    list.appendChild(el);
  });
}

// ── host transfer notice ────────────────────────────────────────────────────
function showHostToast(msg) {
  const el = document.createElement('div');
  el.className = 'fixed top-16 left-1/2 z-[997] card px-4 py-2 rounded-full text-xs font-bold slide-up shadow-xl';
  el.style.transform = 'translateX(-50%)';
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

// ── pre-fill room code from URL ─────────────────────────────────────────────
(function checkUrlRoom() {
  try {
    const p = new URLSearchParams(window.location.search);
    const r = p.get('room');
    if (r) { $('in-room').value = r.toUpperCase(); }
  } catch(e) {}
})();
function triggerNextRound() { socket.emit('next_round', {room:myRoom}); }
function confirmNextRound() {
  const payload = {room:myRoom};
  if (selectedMidMode) payload.new_mode = selectedMidMode;
  socket.emit('confirm_next_round', payload);
  selectedMidMode = null;
}

function renderScores(scores) {
  const medals = ['🥇','🥈','🥉'];
  return scores.filter(p=>!p.is_spectator).map((p,i) => `
    <div class="flex items-center gap-3 p-3 rounded-xl ${i===0?'border':'card'}" style="${i===0?'background:rgba(234,179,8,.08);border-color:rgba(234,179,8,.3)':''}">
      <span class="text-xl w-7 text-center">${medals[i]||`${i+1}.`}</span>
      <span class="text-2xl">${p.avatar}</span>
      <span class="flex-1 font-bold ${p.id===socket.id?'accent-text':''}">${p.name}</span>
      <span class="font-black text-xl ${i===0?'text-yellow-400':''}">${p.score}</span>
    </div>`).join('');
}
function updateLeaderBadge(scores) {
  const top = scores.filter(p=>!p.is_spectator)[0];
  if (top && top.score>0) { $('leader-name').innerText=top.name; $('leader-score').innerText=top.score; show('leader-badge'); }
}

// ── story book ──────────────────────────────────────────────────────────────
function renderStoryBook(attributedStories) {
  const book = $('story-book'); book.innerHTML='';
  attributedStories.forEach((st,i) => {
    const page = document.createElement('div');
    page.className = 'card p-5 rounded-2xl book-page';
    page.style.animationDelay = (i*0.15)+'s';
    const header = document.createElement('div');
    header.className = 'flex items-center gap-2 mb-3';
    header.innerHTML = `<span class="text-[9px] accent-bg text-white px-2 py-0.5 rounded font-bold">TUR ${i+1}</span>`;
    page.appendChild(header);
    const storyP = document.createElement('p');
    storyP.className = 'font-serif leading-relaxed mb-4 text-sm';
    storyP.textContent = '"'+st.story+'"';
    page.appendChild(storyP);
    const breakdown = document.createElement('div');
    breakdown.className = 'space-y-1 border-t pt-3 hidden';
    breakdown.style.borderColor = 'rgba(255,255,255,.08)';
    st.attributed_answers.forEach(a => {
      const row = document.createElement('div');
      row.className = 'flex items-center gap-2 text-xs';
      row.innerHTML = `<span class="opacity-40 shrink-0 w-20 truncate">${a.question}</span>
        <span class="opacity-30">→</span>
        <span class="font-bold truncate">"${a.text}"</span>
        <span class="ml-auto opacity-30 shrink-0">${a.owner_avatar}</span>`;
      breakdown.appendChild(row);
    });
    const toggleBtn = document.createElement('button');
    toggleBtn.className = 'text-[9px] opacity-30 hover:opacity-60 mt-2 transition';
    toggleBtn.textContent = '▼ Cevapları göster';
    toggleBtn.onclick = () => {
      const vis = !breakdown.classList.contains('hidden');
      breakdown.classList.toggle('hidden', vis);
      toggleBtn.textContent = vis ? '▼ Cevapları göster' : '▲ Gizle';
    };
    page.appendChild(toggleBtn);
    page.appendChild(breakdown);
    book.appendChild(page);
  });
}

// ── share ────────────────────────────────────────────────────────────────────
function shareWhatsapp() { window.open('https://wa.me/?text='+encodeURIComponent('Kim Kimle oynadık!\n\n"'+lastStory+'"\n\nSen de oyna!')); }
function shareTwitter() { window.open('https://twitter.com/intent/tweet?text='+encodeURIComponent('Kim Kimle? — "'+lastStory+'"\n\n#KimKimle')); }

// ── confetti ─────────────────────────────────────────────────────────────────
function launchConfetti() {
  const colors = ['#f59e0b','#ef4444','#8b5cf6','#06b6d4','#10b981','#f97316'];
  for (let i=0; i<80; i++) { setTimeout(()=>{
    const el = document.createElement('div'); el.className='confetti-piece';
    el.style.left = Math.random()*100+'vw'; el.style.top='-8vh';
    el.style.backgroundColor = colors[Math.floor(Math.random()*colors.length)];
    el.style.borderRadius = Math.random()>.5?'50%':'2px';
    const sz = Math.random()*9+5; el.style.width=el.style.height=sz+'px';
    el.style.animationDuration = (Math.random()*2+2)+'s';
    el.style.animationDelay = (Math.random()*.8)+'s';
    document.body.appendChild(el); setTimeout(()=>el.remove(), 5000);
  }, i*20); }
}

// ══════════════════════════════════════════════════════════════════
// SOCKET LISTENERS
// ══════════════════════════════════════════════════════════════════

socket.on('room_created', d => {
  myRoom = d.room; isSpectator = d.spectator||false; isHost = d.is_host||false;
  $('room-display').innerText = myRoom;
  show('room-display'); show('oda-label');
  saveSession();
  showScreen('screen-lobby');
  hide('host-controls'); hide('guest-waiting'); hide('spectator-waiting');
  if (isSpectator) show('spectator-waiting');
  else if (isHost) show('host-controls');
  else show('guest-waiting');
  if (d.rejoin) { show('btn-history'); }
});

socket.on('update_list', list => {
  const active = list.filter(p=>!p.is_spectator && !p.disconnected);
  const specs = list.filter(p=>p.is_spectator);
  const dc = list.filter(p=>p.disconnected);
  $('player-count').innerText = active.length+'/7'+(specs.length?` + ${specs.length}👁️`:'')+(dc.length?` (${dc.length}💤)`:'');
  if (isHost) { show('kick-hint'); }
  $('list-players').innerHTML = list.map(p => {
    const isMe = p.id === socket.id;
    const isDC = p.disconnected;
    const canKick = isHost && !isMe && !p.is_spectator;
    return `<div class="flex items-center gap-3 card p-2 rounded-lg ${isDC?'opacity-50':''}"
      ${canKick ? `oncontextmenu="kickPlayer('${p.id}','${p.name}');return false;"
        ontouchstart="startKickPress('${p.id}','${p.name}')" ontouchend="cancelKickPress()"` : ''}>
      <span class="text-xl">${p.avatar}</span>
      <span class="text-sm font-bold ${isMe?'accent-text':''}${isDC?' line-through':''}">${p.name}</span>
      ${p.is_spectator ? '<span class="spec-badge ml-auto">İZLEYİCİ</span>' : ''}
      ${isDC ? '<span class="dc-badge ml-auto">💤 BAĞLANTI KESİLDİ</span>' : ''}
      ${canKick && !isDC ? `<button onclick="kickPlayer('${p.id}','${p.name}')" class="ml-auto text-[9px] opacity-0 hover:opacity-60 text-red-400 font-bold px-2 py-1 rounded kick-btn transition">✕ AT</button>` : ''}
    </div>`;
  }).join('');
  // Show kick buttons on hover via CSS trick — add hover class to parent
  document.querySelectorAll('[oncontextmenu]').forEach(el => {
    el.addEventListener('mouseenter', () => el.querySelector('.kick-btn')?.classList.replace('opacity-0','opacity-60'));
    el.addEventListener('mouseleave', () => el.querySelector('.kick-btn')?.classList.replace('opacity-60','opacity-0'));
  });
});

socket.on('settings_changed', d => {
  if (d.key==='mode') {
    currentMode = d.val;
    const label = d.config?.label || d.val;
    $('mode-badge').innerText = label;
    if ($('display-mode')) $('display-mode').innerText = label;
    applyGameTheme(d.val);
  } else if (d.key==='show_author') {
    if ($('display-visibility')) $('display-visibility').innerText = d.val?'İsimler Açık 👁️':'İsimler Gizli 🕵️';
  } else if (d.key==='custom_questions') {
    currentMode = 'custom';
  }
});

socket.on('game_start', d => {
  currentMode = d.mode; totalRounds = d.total_rounds; currentQCount = d.q_count||7;
  completedStories = []; allStories = [];
  show('btn-history');
  if (isSpectator) { showScreen('screen-spectator'); show('round-indicator'); }
  else { showScreen(currentMode==='parallel'?'screen-parallel':'screen-game'); show('round-indicator'); }
});

socket.on('round_start', d => {
  currentRound = d.round; currentQCount = d.q_count||7;
  if (d.mode) { currentMode = d.mode; applyGameTheme(d.mode); }
  $('round-num').innerText = d.round+'/'+d.total;
  if (isSpectator) showScreen('screen-spectator');
  else showScreen(currentMode==='parallel'?'screen-parallel':'screen-game');
  hide('turn-active'); hide('turn-wait'); hide('timer-wrap'); hide('skip-toast');
  hide('p-answering'); hide('p-waiting'); hide('p-voting'); hide('p-timer-wrap');
});

socket.on('turn_data', d => {
  if (isSpectator) {
    $('spec-avatar').innerText = d.active_avatar;
    $('spec-text').innerText = d.active_name+' düşünüyor...';
    $('spec-question').innerText = d.q;
    return;
  }
  hide('skip-toast');
  if (socket.id === d.active_id) {
    show('turn-active'); hide('turn-wait');
    $('step-badge').innerText = `SORU ${d.step+1}/${d.total_q||7}`;
    $('q-text').innerText = d.q; $('in-ans').value='';
    // Mobile keyboard + scroll fix
    setTimeout(() => {
      const inp = $('in-ans');
      if (inp) { inp.focus(); inp.scrollIntoView({behavior:'smooth',block:'center'}); }
    }, 100);
    startClassicTimer(d.timer||20);
    // Host skip even on own turn
    if (isHost) show('btn-skip'); else hide('btn-skip');
  } else {
    hide('turn-active'); show('turn-wait'); stopClassicTimer();
    $('wait-avatar').innerText = d.active_avatar;
    $('wait-text').innerText = d.active_name+' düşünüyor...';
    if (isHost) show('btn-skip'); else hide('btn-skip');
  }
});

socket.on('question_skipped', d => {
  const toast = $('skip-toast');
  toast.innerText = `⏭ Soru atlandı`;
  show('skip-toast');
  setTimeout(()=>hide('skip-toast'), 2000);
});

socket.on('p_round_start', d => {
  if (isSpectator) {
    $('spec-question').innerText = d.q;
    $('spec-text').innerText = 'Herkes cevaplıyor...'; $('spec-avatar').innerText = '⚡';
    return;
  }
  hide('p-waiting'); hide('p-voting');
  $('p-step-badge').innerText = `SORU ${d.step+1}/${d.total_q||7}`;
  $('p-q-text').innerText = d.q; $('in-p-ans').value='';
  show('p-answering');
  setTimeout(() => {
    const inp = $('in-p-ans');
    if (inp) { inp.focus(); inp.scrollIntoView({behavior:'smooth',block:'center'}); }
  }, 100);
  startParallelTimer(d.timer||20);
});

socket.on('p_answer_count', d => {
  $('p-answer-count').innerText = `${d.count}/${d.total} cevapladı`;
  // Build dot indicators
  const avatars = $('p-answer-avatars');
  if (avatars) {
    avatars.innerHTML = '';
    for (let i=0; i<d.total; i++) {
      const dot = document.createElement('div');
      dot.className = 'w-2 h-2 rounded-full transition-all duration-300';
      dot.style.background = i < d.count ? 'var(--primary)' : 'rgba(255,255,255,.2)';
      avatars.appendChild(dot);
    }
  }
});

socket.on('p_vote_start', d => {
  if (isSpectator) { $('spec-text').innerText='Oylama sürüyor...'; $('spec-avatar').innerText='🗳️'; return; }
  stopParallelTimer(); hide('p-answering'); hide('p-waiting'); show('p-voting');
  $('vote-info').innerHTML = d.is_tie ? "<span style='color:#f87171;font-weight:bold'>BERABERLİK! Tekrar seç!</span>" : "En iyi cevabı seç!";
  $('vote-list').innerHTML='';
  d.candidates.forEach(c => {
    const mine = c.owner_id === socket.id;
    const btn = document.createElement('button');
    btn.className = 'w-full p-4 rounded-xl text-left font-bold text-sm flex justify-between items-center transition card '
      +(mine?'opacity-50 cursor-not-allowed':'hover:bg-white/15 cursor-pointer');
    btn.innerHTML = `<div>"${c.text}"${c.name?` <span style="font-size:.6rem;opacity:.5">(${c.name})</span>`:''}</div>
      ${mine?'<span style="font-size:.6rem;opacity:.4;text-transform:uppercase">(SEN)</span>':''}`;
    if (!mine) btn.onclick = ()=>castVote(c.owner_id);
    $('vote-list').appendChild(btn);
  });
});

socket.on('story_reveal', d => {
  currentAttributedAnswers = d.attributed_answers; lastStory = d.story;
  completedStories.push({story:d.story, attributed_answers:d.attributed_answers, round:d.round});
  showScreen('screen-reveal');
  $('reveal-round-badge').innerText = `TUR ${d.round}/${d.total_rounds} HİKAYESİ`;
  hide('reveal-reactions'); hide('reveal-vote'); hide('reveal-result'); hide('vote-submitted-msg');
  revealStory(d.story, d.attributed_answers);
});

socket.on('emoji_broadcast', d => {
  spawnEmoji(d.emoji, d.is_spectator);
  playSound('pop');
});

socket.on('vote_result', d => {
  let winText = 'Kimse oy almadı 😔';
  let catText = '';
  if (d.winner_id) {
    const w = currentAttributedAnswers.find(a=>a.owner_id===d.winner_id);
    if (w) {
      winText = `🏆 ${w.owner_avatar} ${w.owner_name} +1 puan aldı!`;
      if (d.winner_category) catText = `${d.winner_category} En ${d.winner_category==='😂'?'Komik':d.winner_category==='💡'?'Yaratıcı':'Şok Edici'} Cevap seçildi`;
      if (d.winner_id===socket.id) {
        playSound('win');
        spawnEmoji('🏆'); setTimeout(()=>spawnEmoji('⭐'),200); setTimeout(()=>spawnEmoji('🎉'),400);
      }
    }
  }
  $('winner-text').innerText = winText;
  $('winner-category').innerText = catText;
  updateLeaderBadge(d.scores);
  hide('reveal-vote'); hide('vote-submitted-msg'); show('reveal-result');
  const isLast = (d.round+1) >= d.total_rounds;
  if (isHost) {
    hide('btn-next-round'); hide('btn-final-results'); hide('waiting-next');
    isLast ? show('btn-final-results') : show('btn-next-round');
  } else {
    hide('btn-next-round'); hide('btn-final-results'); show('waiting-next');
  }
});

socket.on('show_scores', d => {
  showScreen('screen-scores');
  $('scores-subtitle').innerText = `Tur ${d.round} bitti!`;
  $('scores-list').innerHTML = renderScores(d.scores);
  $('next-round-num').innerText = d.round+1;
  updateLeaderBadge(d.scores);
  selectedMidMode = null;
  if (isHost) {
    show('scores-host-btn'); hide('scores-guest-wait'); show('scores-mode-switch');
    hide('mid-mode-selected');
    document.querySelectorAll('.mid-mode-btn').forEach(b=>b.classList.remove('accent-bg','text-white'));
  } else {
    hide('scores-host-btn'); show('scores-guest-wait'); hide('scores-mode-switch');
  }
});

socket.on('game_final', d => {
  lastStory = d.stories[d.stories.length-1]||'';
  allStories = d.attributed_stories||[];
  showScreen('screen-final'); launchConfetti(); playSound('win');
  const champ = d.scores.filter(p=>!p.is_spectator)[0];
  if (champ) {
    $('champion-avatar').innerText = champ.avatar;
    $('champion-name').innerText = champ.name+(champ.id===socket.id?' 🫵 SEN!':'');
    $('champion-score').innerText = champ.score+' puan ile şampiyon!';
  }
  $('final-scores').innerHTML = renderScores(d.scores);
  renderStoryBook(allStories);
  // Clear session on game end
  try { sessionStorage.removeItem('kkv8'); } catch(e) {}
});

socket.on('connect', () => {
  show('ui-header');
  hide('round-indicator');
});

// ── typing indicator ────────────────────────────────────────────────────────
socket.on('player_typing', d => {
  const ind = $('typing-indicator');
  if (!ind) return;
  if (d.is_typing) show('typing-indicator');
  else hide('typing-indicator');
});

// ── quick phrases ───────────────────────────────────────────────────────────
socket.on('phrase_broadcast', d => {
  spawnPhraseToast(d.text, d.avatar, false);
  playSound('pop');
});

// ── kick / disconnect / host transfer ──────────────────────────────────────
socket.on('you_were_kicked', () => {
  alert('Odadan atıldın! 🚪');
  try { sessionStorage.removeItem('kkv8'); } catch(e) {}
  location.reload();
});
socket.on('player_kicked', d => {
  showHostToast(`🚪 ${d.name} odadan atıldı`);
});
socket.on('player_disconnect', d => {
  showHostToast(`💤 ${d.avatar} ${d.name} bağlantısı kesildi (${d.grace}s)`);
});
socket.on('player_left', d => {
  showHostToast(`👋 ${d.avatar} ${d.name} ayrıldı`);
});
socket.on('player_rejoin', d => {
  showHostToast(`✅ ${d.name} geri döndü!`);
  playSound('pop');
});
socket.on('host_transfer', d => {
  if (d.new_host_id === socket.id) {
    isHost = true;
    showHostToast('👑 Sen host oldun!');
    // Show host controls if in lobby
    if (!$('screen-lobby').classList.contains('hidden')) {
      show('host-controls'); hide('guest-waiting');
    }
  } else {
    showHostToast(`👑 ${d.new_host_name} yeni host`);
  }
});
</script>
</body>
</html>
{% endraw %}"""

if __name__ == '__main__':
    socketio.run(app, debug=True, port=5000)
