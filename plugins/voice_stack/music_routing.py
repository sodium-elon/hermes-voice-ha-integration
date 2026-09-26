"""Jev chooses operations/source spans; code owns all evidence and execution.

No catalog, artist index, or hotword lookups happen before select_route.
Confidence is a gate, not proof. Re-decodes of one WAV are correlated evidence.
"""
from dataclasses import dataclass
import math
import re
import time

FLOOR = .6
OPERATIONS = {
    'named_song': 'Play a specifically named song; Say Something can be a song title, never reinterpret as top song',
    'named_album': 'Play a specifically named album',
    'artist': 'Play music by a named performer',
    'top_song': 'Explicit best, top or most popular song request, no specific title',
    'top_album': 'Explicit best, top or most popular album request, no specific title',
    'latest_song': 'Newest or latest song by a performer',
    'latest_album': 'Newest or latest album by a performer',
    'genre': 'Genre music, optionally restricted to a year or decade; no performer required',
    'playlist': 'A specifically named playlist',
    'music': 'Generic music, no specific artist, genre, song, or playlist',
    'collab': 'Music by multiple performers together; every performer must be retained',
    'described_song': 'Song identified by lyrics, scene or description rather than a title',
    'clarify': 'Music playback but insufficient or conflicting information; incomplete reference',
    'nonmusic': 'Not a music playback request, including ordinary conversation and device control',
}


def probability(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and 0 <= value <= 1 else 0.
    except (TypeError, ValueError):
        return 0.


@dataclass
class FollowUp:
    """Owned by one callback/session, never a module-global conversation cache."""
    artist: str = ''
    wake_id: object = None
    expires: float = 0.

    def read(self, wake_id):
        if wake_id != self.wake_id or time.monotonic() >= self.expires:
            self.artist = ''
        return self.artist

    def remember(self, artist, wake_id):
        self.artist, self.wake_id = artist, wake_id
        self.expires = time.monotonic() + 30.


def source_spans(text):
    """Enumerate bounded contiguous source spans, not generated names or facts."""
    tokens = list(re.finditer(r"\S+", text))
    if len(tokens) > 20:
        return {}
    spans = {}
    for start in range(len(tokens)):
        for end in range(start, len(tokens)):
            value = text[tokens[start].start():tokens[end].end()].strip(' .,!?:"')
            if value and value not in spans.values():
                spans[f's{len(spans)}'] = value
    return spans


def select_route(text, *, confidence=1., pending_artist=''):
    from . import music
    from typesafe_sdk import Choice, Noul, TypeSafeClient
    spans = source_spans(text)
    if not spans:
        return {'operation': 'clarify', 'confidence': 0.}
    key = music._load_api_key()
    if not key:
        raise music.TypeSafeUnavailable(music.UNAVAILABLE)
    options = {'none': 'Not present or cannot identify a complete source span', **spans}
    artist_options = dict(options)
    if pending_artist:
        artist_options['pending'] = f'Previously pending artist: {pending_artist}; only for a follow-up referring to that artist'
    questions = {
        'operation': Choice(instructions='Select the operation requested in `request`. Never repair or reinterpret words as facts. Use pending artist only for a clear follow-up.', criteria=OPERATIONS),
        'artist': Choice(instructions='Select the exact span naming the primary performer (not song/title/genre). If absent select none, or pending for a clear follow-up.', criteria=artist_options),
        'secondary': Choice(instructions='For a collaboration, select the exact span naming the second performer, excluding joining words. Otherwise none.', criteria=options),
        'title': Choice(instructions='For a named song/album choose its exact title span, excluding performer and command words. Otherwise none.', criteria=options),
        'genre': Choice(instructions='For genre playback select the genre alone, excluding music and date words. Otherwise none.', criteria=options),
        'period': Choice(instructions='Select only the explicitly requested year or decade, e.g. 2026 or 1980s. Otherwise none.', criteria=options),
        'complete': Noul(instructions='Can the request be represented by ONE operation and at most two performers, without losing any required constraints or chained actions? Reject three or more performers and secondary device actions.'),
    }
    try:
        with TypeSafeClient(api_key=key) as client:
            response = client.system_one(model='jev-latest', state={'request': text, 'stt_confidence': probability(confidence), 'pending_artist': pending_artist, 'source_spans': spans}, questions=questions)
        answers = response.answers
        op = answers['operation']
        operation = str(op.choice)
        trace = {name: {'choice': getattr(answer, 'choice', None), 'confidence': probability(getattr(answer, 'confidence', 0)), 'probabilities': getattr(answer, 'probabilities', None), 'noul': getattr(answer, 'noul', None)} for name, answer in answers.items()}
        result = {'operation': operation, 'confidence': probability(op.confidence), 'judgments': trace}
        required = {'artist': ['artist'], 'named_song': ['title'], 'named_album': ['title'], 'top_song': ['artist'], 'top_album': ['artist'], 'latest_song': ['artist'], 'latest_album': ['artist'], 'genre': ['genre'], 'playlist': ['title'], 'music': [], 'collab': ['artist', 'secondary']}.get(operation, [])
        for name in ('artist', 'secondary', 'title', 'genre', 'period'):
            answer = answers[name]
            choice = str(answer.choice)
            value = pending_artist if name == 'artist' and choice == 'pending' else spans.get(choice, '')
            result[name] = value
            if (name in required or value) and probability(answer.confidence) < FLOOR:
                result['operation'] = 'clarify'
            if name in required and not value:
                result['operation'] = 'clarify'
        if result['confidence'] < FLOOR or probability(answers['complete'].noul) < .8:
            result['operation'] = 'clarify'
        music._write_telemetry({'outcome': 'jev_route', **result})
        return result
    except music.TypeSafeUnavailable:
        raise
    except Exception:
        raise music.TypeSafeUnavailable(music.UNAVAILABLE) from None


def execute(route, text, confidence, *, followup=None, wake_id=None):
    from . import music
    op = route.get('operation', 'clarify')
    artist = music._safe_name(route.get('artist')) or ''
    if followup is not None and artist:
        followup.remember(artist, wake_id)
    if op == 'nonmusic':
        return {'nonmusic': True}
    if op == 'clarify' or probability(route.get('confidence')) < FLOOR:
        return dict(music.CLARIFY)
    # Semantic certainty cannot manufacture acoustic evidence. Same-WAV agreement
    # is not an exemption. Ask for a fresh utterance when no pass clears the floor.
    if probability(confidence) < FLOOR:
        return {'clarification': "I didn't catch that clearly. Could you repeat the music request?"}
    intent = {'shuffle': text.lower().startswith('shuffle'), 'research': False, 'latest': False, 'studio': False}
    lookup = 'none'
    result = None
    if op == 'genre':
        genre = music._safe_name(route.get('genre'))
        period = route.get('period') or ''
        verb = 'Shuffle' if intent['shuffle'] else 'Play'
        if genre and (not period or re.fullmatch(r'(?:19|20)\d{2}s?', period)):
            result = {'command': f'{verb} {genre} music' + (f' from {period}' if period else '') + ' on Apple Music'}
            lookup = 'literal_source_spans'
    elif op == 'playlist':
        title = music._safe_name(route.get('title')) or music._safe_name(route.get('genre'))
        if title:
            result = {'command': f'Play the playlist {title} on Apple Music'}
            lookup = 'literal_source_spans'
    elif op == 'music':
        result = {'command': 'Shuffle music on Apple Music' if intent['shuffle'] else 'Play music on Apple Music'}
        lookup = 'generic_no_lookup'
    elif op in {'top_song', 'top_album'}:
        # iTunes search order is relevance, NOT popularity. Without chart evidence
        # we cannot honestly identify a top release. Keep artist for follow-up.
        result = {'clarification': f"I can't verify popularity rankings for {artist}. Would you like songs by {artist} instead?"}
        lookup = 'popularity_unavailable_no_search_rank_substitution'
    elif op in {'latest_song', 'latest_album'}:
        lookup = 'itunes_releaseDate_exact_artist'
        result = (music._resolve_latest_track if op == 'latest_song' else music._resolve_latest_album)(artist)
    elif op == 'artist':
        lookup = 'itunes_exact_artist'
        result = music._resolve_artist(intent, artist)
        if not result:
            # The exact catalog resolve missed. When one way fails, try the
            # other before asking for a repeat: the garble may be confidently
            # wrong ("Bill McCartney" for "Dolly Parton"), so recover from the
            # DB + full listening vocabulary. Nothing to gain from a round of
            # 'wrong word' paraphrase that cannot cross the phonetic gap.
            lookup = 'db_vocab_artist_fallback'
            result = music._artist_db_fallback(artist, confidence)
    elif op == 'named_song':
        lookup = 'itunes_exact_title_and_artist'
        result = music._resolve_song(intent, route.get('title', ''), artist)
    elif op == 'named_album':
        lookup = 'itunes_exact_album_and_artist'
        rows = music._catalog(term=route.get('title', ''), entity='album')
        matches = [r for r in rows if music._normalize_artist_key(r.get('collectionName')) == music._normalize_artist_key(route.get('title')) and (not artist or music._normalize_artist_key(r.get('artistName')) == music._normalize_artist_key(artist))]
        if matches:
            row = matches[0]
            if music._safe_name(row.get('collectionName')) and music._safe_name(row.get('artistName')):
                result = music._album_command(row['collectionName'], row['artistName'])
    elif op == 'collab':
        lookup = 'db_verified_all_artist_slots'
        secondary = music._safe_name(route.get('secondary'))
        if artist and secondary and artist.casefold() != secondary.casefold():
            result = music._resolve_collab_artists(artist, secondary, text, confidence)
    elif op == 'described_song':
        # Catalog name matches do not prove lyric/scene facts. No generated facts.
        lookup = 'description_evidence_unavailable'
        result = {'clarification': 'Could you name the song or give its artist?'}
    music._write_telemetry({'outcome': 'route_execution', 'operation': op, 'lookup': lookup, 'source': route, 'result': result})
    return result or dict(music.CLARIFY)
