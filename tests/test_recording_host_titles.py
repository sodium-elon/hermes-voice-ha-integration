"""Fresh recorded sessions must satisfy the real Hermes title uniqueness rule."""
import sys
import requests
from plugins.voice_stack import sessions_api
from test_recording_sessions_api import recording, prompt, Response, stream


def test_recorded_sessions_survive_real_host_title_uniqueness(recording, monkeypatch, tmp_path):
    sys.path.insert(0, '/home/john/.hermes/hermes-agent')
    from hermes_state import SessionDB
    db = SessionDB(tmp_path / 'isolated-state.db')
    db.create_session('old-voice', 'api_server')
    db.set_session_title('old-voice', 'Voice (Hey Jarvis)')
    created=[]
    def post(url, **kwargs):
        if url.endswith('/api/sessions'):
            body=kwargs['json'];sid=body['id'];db.create_session(sid, 'api_server')
            response=Response()
            try:
                db.set_session_title(sid, body['title'])
            except ValueError:
                response.status_code=400;response.text='invalid_title';return response
            created.append((sid, body['title']))
            return response
        return Response(stream(('assistant.completed', {'content':'Done.'}),
                               ('run.completed', {'completed':True})))
    monkeypatch.setattr(requests,'post',post)
    try:
        for _ in range(2):
            assert sessions_api.complete(prompt(recording),profile='music')=='Done.'
        assert len({title for _,title in created})==2
    finally:
        db.close()
