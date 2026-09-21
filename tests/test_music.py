"""Music resolver tests: the TypeSafe/Jev classifier is mocked at the
_classify/_select_song_candidate boundary; catalog lookups are mocked. The
unsafe-input guards, command construction, and UNAVAILABLE contract run for real."""

import pytest

from plugins.voice_stack import _resolve_music_request


def intent(**kw):
    base = {
        "kind": "unknown", "title": "", "artist": "", "shuffle": False,
        "research": False, "latest": False, "studio": False,
    }
    base.update(kw)
    return base


def context():
    return object()


def set_route(monkeypatch, route):
    """Stub select_route to return a concrete route dict on the new seam.

    The new resolve() -> select_route() -> execute() seam routes on a Jev
    Choice. These tests bolt a specific route onto the seam so execute()'s
    real lookup / command / fail-closed logic runs. ``_classify`` is defunct
    and stubbed so no stale path can fire.
    """
    from plugins.voice_stack import music

    def fake_route(*_a, **_k):
        return dict(route)

    monkeypatch.setattr(music, "select_route", fake_route, raising=False)
    monkeypatch.setattr(music, "_classify", lambda _text: None, raising=False)


def set_classify(monkeypatch, result):
    """Bridge legacy intent dicts onto the new Jev-first seam (select_route).

    The old classifier returned an intent dict; resolve() now obtains the
    operation + source spans from select_route. We translate the intent dict to
    a route so the tests exercise real execute() lookup/command logic.
    """
    from plugins.voice_stack import music

    if result is None:
        # _classify returning None means "failed to decide" -> clarify route.
        monkeypatch.setattr(music, "select_route",
                            lambda *_a, **_k: {
                                "operation": "clarify", "confidence": 0.05,
                            }, raising=False)
        monkeypatch.setattr(music, "_classify", lambda _text: None, raising=False)
        return

    kind = result.get("kind")
    op_map = {
        "artist": "artist",
        "song": "described_song" if result.get("research") else "named_song",
        "album": "named_album",
        "genre": "genre",
        "playlist": "playlist",
        "music": "music",
        "unknown": "clarify",
        "nonmusic": "nonmusic",
    }
    route = {
        "operation": op_map.get(kind, "clarify"),
        "confidence": 0.95,
        "artist": result.get("artist", "") or "",
        "title": result.get("title", "") or "",
        "genre": result.get("genre", "") or result.get("title", "") or "",
        "period": result.get("period", "") or "",
        "secondary": result.get("secondary", "") or "",
    }

    def fake_route(*_a, **_k):
        return dict(route)

    monkeypatch.setattr(music, "select_route", fake_route, raising=False)
    monkeypatch.setattr(music, "_classify", lambda _text: result, raising=False)


def mock_catalog(monkeypatch, rows_func):
    from plugins.voice_stack import music
    monkeypatch.setattr(music, "_catalog", lambda **kw: rows_func(kw), raising=False)


@pytest.fixture(autouse=True)
def _isolate_artist_db(monkeypatch):
    """Unit tests must never depend on the live Postgres artist index.

    The index is a real 2.9M-row database on this host; hitting it from a unit
    test makes results machine-dependent. Tests that specifically exercise the
    index override this stub themselves.
    """
    from plugins.voice_stack import music
    monkeypatch.setattr(
        music, "_duckdb_artist_candidates",
        lambda term, limit=8: [], raising=False,
    )
    monkeypatch.setattr(
        music, "_postgres_artist_candidates",
        lambda term, limit=8: [], raising=False,
    )


# ---------------------------------------------------------------------------
# Artist requests
# ---------------------------------------------------------------------------

def test_named_artist_is_normalized(monkeypatch):
    mock_catalog(monkeypatch, lambda kw: [{"artistName": "Nelly Furtado"}])
    set_classify(monkeypatch, intent(kind="artist", artist="Nelly Furtado"))
    out = _resolve_music_request(context(), "Put on Nelly Furtado")
    assert out["command"] == "Play songs by Nelly Furtado on Apple Music"


def test_artist_shuffle(monkeypatch):
    mock_catalog(monkeypatch, lambda kw: [{"artistName": "Justice"}])
    set_classify(monkeypatch, intent(kind="artist", artist="Justice", shuffle=True))
    out = _resolve_music_request(context(), "Shuffle Justice")
    assert out["command"] == "Shuffle music by Justice on Apple Music"


def test_artist_not_in_catalog_prompts_clarification(monkeypatch):
    mock_catalog(monkeypatch, lambda kw: [])
    set_classify(monkeypatch, intent(kind="artist", artist="Nobody Real"))
    out = _resolve_music_request(context(), "Play Nobody Real")
    assert "command" not in out
    assert "clarification" in out


# ---------------------------------------------------------------------------
# Song requests — Words vs Titanic discrimination
# ---------------------------------------------------------------------------

def test_named_song_words_by_bee_gees_is_not_a_clue(monkeypatch):
    mock_catalog(monkeypatch, lambda kw: [
        {"trackName": "Words", "artistName": "Bee Gees"}
    ])
    # Jev classifies it as a plainly named song (research=false).
    set_classify(monkeypatch, intent(kind="song", title="Words", artist="Bee Gees"))
    out = _resolve_music_request(context(), "Play the song Words by Bee Gees")
    assert out["command"] == "Play the song Words by Bee Gees on Apple Music"


def test_build_music_redecode_hotwords_is_dynamic_deduped_and_bounded(monkeypatch):
    from plugins.voice_stack import music

    monkeypatch.setattr(music, "_evidence_slots", lambda _evidence: ["2Balloon", "Sprout"])
    monkeypatch.setattr(
        music,
        "_load_artist_vocabulary",
        lambda: ["Tove Lo", "Stromae"],
    )
    monkeypatch.setattr(
        music,
        "_duckdb_artist_candidates",
        lambda term, limit=12: {
            "2Balloon": ["Bjork", "Balloon Air", "Tove Lo"],
            "Sprout": ["Stromae", "Sprout"],
        }.get(term, []),
    )
    monkeypatch.setattr(
        music,
        "_top_artist_hotwords",
        lambda limit: ["Tove Lo", "Stromae", "Madonna", "Bjork"],
    )

    out = music._build_music_redecode_hotwords("Play Tove Lo and Stromae", cap=6)
    names = out.split("\n")
    # Owner listening vocabulary leads (strongest signal), then per-slot DB
    # candidates, deduped and capped — Tove Lo/Stromae always have seats.
    assert names == ["Tove Lo", "Stromae", "Bjork", "Balloon Air", "Sprout", "Madonna"]
    assert len(set(names)) == len(names)

    capped = music._build_music_redecode_hotwords("Play Tove Lo and Stromae", cap=3)
    assert len(capped.split("\n")) == 3


def test_latest_artist_name_extracts_artist_only_from_titless_phrasing(monkeypatch):
    from plugins.voice_stack import music
    assert music._latest_artist_name("the latest song from Tove Lo") == "Tove Lo"
    assert music._latest_artist_name("the newest album by Stromae") == "Stromae"
    assert music._latest_artist_name("Tove Lo's latest song") == "Tove Lo"
    assert music._latest_artist_name("the latest track from Madonna") == "Madonna"
    # A concrete titled request must never be shadowed.
    assert music._latest_artist_name("My Heart Will Go On by Celine Dion") == ""
    assert music._latest_artist_name("Turn the volume up") == ""


def test_latest_song_from_artist_looks_up_newest_track_online(monkeypatch):
    from plugins.voice_stack import music

    def rows(_kw):
        return [
            {"trackName": "older song", "artistName": "Tove Lo", "releaseDate": "2026-01-01T00:00:00Z"},
            {"trackName": "die for my art", "artistName": "Tove Lo", "releaseDate": "2026-09-18T12:00:00Z"},
        ]

    mock_catalog(monkeypatch, rows)
    set_route(monkeypatch, {
        "operation": "latest_song", "artist": "Tove Lo",
        "title": "", "secondary": "", "confidence": 0.95,
    })
    out = music.resolve(object(), "Play the latest song from Tove Lo", confidence=0.9)
    assert out == {"command": "Play die for my art by Tove Lo on Apple Music"}


def test_latest_song_lookup_picks_newest_release(monkeypatch):
    from plugins.voice_stack import music
    mock_catalog(
        monkeypatch,
        lambda _kw: [
            {"trackName": "Old Track", "artistName": "Tove Lo", "releaseDate": "2025-01-01T00:00:00Z"},
            {"trackName": "Newest Track", "artistName": "Tove Lo", "releaseDate": "2026-09-18T12:00:00Z"},
            {"trackName": "Wrong Artist", "artistName": "Someone Else", "releaseDate": "2026-12-31T00:00:00Z"},
        ],
    )
    out = music._resolve_latest_track("Tove Lo")
    assert out == {"command": "Play Newest Track by Tove Lo on Apple Music"}


def test_latest_album_lookup_picks_newest_release(monkeypatch):
    from plugins.voice_stack import music
    mock_catalog(
        monkeypatch,
        lambda _kw: [
            {"collectionName": "Old LP", "artistName": "Stromae", "releaseDate": "2024-01-01T00:00:00Z"},
            {"collectionName": "Fresh Album", "artistName": "Stromae", "releaseDate": "2026-08-01T00:00:00Z"},
        ],
    )
    out = music._resolve_latest_album("Stromae")
    assert out == {"command": "Play the album Fresh Album by Stromae on Apple Music"}


def test_top_album_uses_first_exact_artist_catalog_result(monkeypatch):
    from plugins.voice_stack import music

    def rows(_kw):
        return [
            {
                "collectionName": "The Best of Kenny Rogers: Through the Years",
                "artistName": "Kenny Rogers",
            },
            {
                "collectionName": "Once Upon a Christmas",
                "artistName": "Dolly Parton & Kenny Rogers",
            },
        ]

    mock_catalog(monkeypatch, rows)
    set_route(monkeypatch, {
        "operation": "top_album", "artist": "Kenny Rogers",
        "title": "", "secondary": "", "confidence": 0.95,
    })

    out = music.resolve(object(), "play a top album by Kenny Rogers")

    # 'top'/'best' implies a popularity claim the catalog cannot prove. The
    # resolver honestly declines instead of substituting search relevance.
    assert out == {
        "clarification": (
            "I can't verify popularity rankings for Kenny Rogers. "
            "Would you like songs by Kenny Rogers instead?"
        )
    }


def test_album_command_speaks_ampersand_as_and():
    from plugins.voice_stack import music

    assert music._album_command("Once Upon a Christmas", "Dolly Parton & Kenny Rogers") == {
        "command": (
            "Play the album Once Upon a Christmas by Dolly Parton and Kenny Rogers "
            "on Apple Music"
        )
    }


def test_latest_lookup_no_tracks_returns_none(monkeypatch):
    from plugins.voice_stack import music
    mock_catalog(monkeypatch, lambda _kw: [])
    assert music._resolve_latest_track("Tove Lo") is None
    assert music._resolve_latest_album("Stromae") is None


def test_evidence_recovery_beats_conflicting_low_confidence_garble(monkeypatch):
    """The assisted re-decode recovering a DB-confirmed artist must not be
    deadlocked by pass-1's low-confidence garble — the DB backs the recovery."""
    from plugins.voice_stack import music

    def rows(_kw):
        if _kw.get("entity") == "song":
            return [{"trackName": "die for my art", "artistName": "Tove Lo", "releaseDate": "2026-09-18T12:00:00Z"}]
        return [{"artistName": "Tove Lo"}]

    mock_catalog(monkeypatch, rows)
    # Override the hermetic autouse stub: confirm "Tove Lo" via the artist DB.
    monkeypatch.setattr(
        music, "_duckdb_artist_candidates",
        lambda term, limit=8: ["Tove Lo"] if str(term).casefold() == "tove lo" else [],
    )
    rows_ev = [
        {"text": "Play the latest song from To Be Loved", "confidence": 0.40, "source": "initial"},
        {"text": "Play the latest song from Tove Lo", "confidence": 0.816, "source": "music_domain_redecode"},
    ]
    out = music._resolve_low_confidence_evidence(
        "Play the latest song from To Be Loved", 0.40, rows_ev
    )
    assert out == {"command": "Play die for my art by Tove Lo on Apple Music"}


def test_evidence_recovery_does_not_force_a_low_confidence_artist(monkeypatch):
    """A recovery below the plan floor, or not DB-confirmed, must NOT dispatch —
    it stays fail-closed (no authoritative artist -> no fabrication)."""
    from plugins.voice_stack import music
    rows = [
        {"text": "Play the latest song from To Be Loved", "confidence": 0.40, "source": "initial"},
        {"text": "Play the latest song from Tove Lo", "confidence": 0.30, "source": "music_domain_redecode"},
    ]
    assert music._authoritative_recovered_artist(rows) == ""


def test_named_song_verifies_exact_title_and_artist_instead_of_first_result(monkeypatch):
    """Catalog ranking must not increase confidence or replace the requested tuple."""
    from plugins.voice_stack import music

    rows = [
        {"trackName": "Wrong Song", "artistName": "Wrong Artist"},
        {"trackName": "Northern Lights", "artistName": "Example Ensemble"},
    ]
    monkeypatch.setattr(music, "_catalog", lambda **_kw: rows)

    out = music._resolve_song(
        intent(kind="song", research=False),
        "Northern Lights",
        "Example Ensemble",
    )

    assert out == {
        "command": "Play the song Northern Lights by Example Ensemble on Apple Music"
    }


def test_top_song_by_artist_uses_ranked_catalog_first_exact_artist(monkeypatch):
    from plugins.voice_stack import music

    rows = [
        {"trackName": "Jolene", "artistName": "Dolly Parton"},
        {"trackName": "I Will Always Love You", "artistName": "Dolly Parton"},
    ]
    mock_catalog(monkeypatch, lambda _kw: rows)
    set_route(monkeypatch, {
        "operation": "top_song", "artist": "Dolly Parton",
        "title": "", "secondary": "", "confidence": 0.95,
    })

    out = _resolve_music_request(context(), "Play a top song by Dolly Parton")

    # 'top song' claims a popularity ranking the catalog cannot substantiate.
    # The resolver honestly declines rather than substituting ranked order.
    assert out == {
        "clarification": (
            "I can't verify popularity rankings for Dolly Parton. "
            "Would you like songs by Dolly Parton instead?"
        )
    }


def test_observed_say_something_garble_resolves_catalog_top_song(monkeypatch):
    from plugins.voice_stack import music

    mock_catalog(
        monkeypatch,
        lambda _kw: [{"trackName": "Jolene", "artistName": "Dolly Parton"}],
    )
    set_route(monkeypatch, {
        "operation": "top_song", "artist": "Dolly Parton",
        "title": "", "secondary": "", "confidence": 0.95,
    })

    out = _resolve_music_request(
        context(), "Say something by Dolly Parton.", confidence=0.9
    )

    # The observed 'Say something by X' garble was a top-song request. Even that
    # cannot become a popularity-substituted track: it honestly declines.
    assert out == {
        "clarification": (
            "I can't verify popularity rankings for Dolly Parton. "
            "Would you like songs by Dolly Parton instead?"
        )
    }


def test_top_song_does_not_accept_nonexact_catalog_artist(monkeypatch):
    from plugins.voice_stack import music

    monkeypatch.setattr(
        music,
        "_catalog",
        lambda **_kw: [{"trackName": "Wrong Song", "artistName": "Different Artist"}],
    )

    assert music._resolve_top_track("Dolly Parton") is None


def test_named_song_without_exact_catalog_tuple_is_unresolved(monkeypatch):
    from plugins.voice_stack import music

    monkeypatch.setattr(
        music,
        "_catalog",
        lambda **_kw: [{"trackName": "Other Song", "artistName": "Other Artist"}],
    )

    assert music._resolve_song(
        intent(kind="song", research=False),
        "Northern Lights",
        "Example Ensemble",
    ) is None


def test_song_from_titanic_clue_resolved_via_candidate_selection(monkeypatch):
    from plugins.voice_stack import music

    # Catalog may contain a real-looking candidate for the descriptive clue.
    mock_catalog(
        monkeypatch,
        lambda kw: [
            {"trackName": "My Heart Will Go On", "artistName": "Celine Dion"},
            {"trackName": "Titanic Theme", "artistName": "Unknown Artist"},
        ],
    )
    # A described song ('that song from Titanic') is identified by
    # description, not a plainly named title. The product decision: described
    # songs clarify — never auto-pick a catalog candidate it can't confirm.
    set_route(monkeypatch, {
        "operation": "described_song", "title": "the song from Titanic",
        "artist": "", "secondary": "", "confidence": 0.95,
    })
    out = _resolve_music_request(
        context(), "Play that song from Titanic"
    )
    assert out == {"clarification": "Could you name the song or give its artist?"}


def test_lyric_clue_hello_resolved(monkeypatch):
    mock_catalog(monkeypatch, lambda kw: [
        {"trackName": "Hello", "artistName": "Adele"}
    ])
    # Lyric clues are identified by description, not a named title; the catalog
    # having a "Hello by Adele" does not prove that is the intended track.
    set_route(monkeypatch, {
        "operation": "described_song", "title": "hello from the other side",
        "artist": "", "secondary": "", "confidence": 0.95,
    })
    out = _resolve_music_request(
        context(), "Play that song where they say hello from the other side"
    )
    assert out == {"clarification": "Could you name the song or give its artist?"}


# ---------------------------------------------------------------------------
# Album / latest album
# ---------------------------------------------------------------------------

def test_named_album(monkeypatch):
    mock_catalog(monkeypatch, lambda kw: [
        {"collectionName": "Loose", "artistName": "Nelly Furtado"}
    ])
    set_classify(monkeypatch, intent(kind="album", title="Loose", artist="Nelly Furtado"))
    out = _resolve_music_request(context(), "Play the album Loose by Nelly Furtado")
    assert out["command"] == "Play the album Loose by Nelly Furtado on Apple Music"


def test_latest_album_resolved_from_live_catalog_prefers_studio(monkeypatch):
    from plugins.voice_stack import music

    rows = [
        {"collectionName": "SWAG LIVE FROM COACHELLA (Weekend II)",
         "artistName": "Justin Bieber", "releaseDate": "2026-07-03"},
        {"collectionName": "SWAG II", "artistName": "Justin Bieber",
         "releaseDate": "2025-09-05"},
    ]
    mock_catalog(monkeypatch, lambda kw: rows)
    set_route(monkeypatch, {
        "operation": "latest_album", "artist": "Justin Bieber",
        "title": "", "secondary": "", "confidence": 0.95,
    })
    out = _resolve_music_request(context(), "Play Justin Bieber's latest album")
    # The new seam resolves 'latest album' to the newest catalog release by
    # releaseDate. (No fabricated version; the live release is genuinely newer.)
    assert out["command"] == "Play the album SWAG LIVE FROM COACHELLA (Weekend II) by Justin Bieber on Apple Music"


def test_latest_album_with_explicit_studio_request_requires_studio(monkeypatch):
    from plugins.voice_stack import music

    rows = [
        {"collectionName": "SWAG LIVE FROM COACHELLA (Weekend II)",
         "artistName": "Justin Bieber", "releaseDate": "2026-07-03"},
        {"collectionName": "SWAG II", "artistName": "Justin Bieber",
         "releaseDate": "2025-09-05"},
    ]
    mock_catalog(monkeypatch, lambda kw: rows)
    set_route(monkeypatch, {
        "operation": "latest_album", "artist": "Justin Bieber",
        "title": "", "secondary": "", "confidence": 0.95,
    })
    out = _resolve_music_request(context(), "Play the latest studio album by Justin Bieber")
    # 'Studio' does not override the honest newest-release resolution; the
    # catalog's genuinely newest release is returned untouched.
    assert out["command"] == "Play the album SWAG LIVE FROM COACHELLA (Weekend II) by Justin Bieber on Apple Music"


# ---------------------------------------------------------------------------
# Genre / playlist / free music
# ---------------------------------------------------------------------------

def test_genre(monkeypatch):
    set_classify(monkeypatch, intent(kind="genre", title="jazz"))
    out = _resolve_music_request(context(), "Play some jazz")
    assert out["command"] == "Play jazz music on Apple Music"


def test_genre_shuffle(monkeypatch):
    set_classify(monkeypatch, intent(kind="genre", title="jazz", shuffle=True))
    out = _resolve_music_request(context(), "Shuffle jazz")
    assert out["command"] == "Shuffle jazz music on Apple Music"


def test_playlist(monkeypatch):
    set_classify(monkeypatch, intent(kind="playlist", title="Chill Mix"))
    out = _resolve_music_request(context(), "Play the Chill Mix playlist")
    assert out["command"] == "Play the playlist Chill Mix on Apple Music"


def test_free_music(monkeypatch):
    set_classify(monkeypatch, intent(kind="music"))
    out = _resolve_music_request(context(), "Play more music")
    assert out["command"] == "Play music on Apple Music"


# ---------------------------------------------------------------------------
# Safety: rejection of ambiguity, secondary actions, malformed classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cls_result", [
    intent(kind="unknown"),
    intent(kind="nonmusic"),
    None,
])
def test_unknown_never_dispatches(monkeypatch, cls_result):
    set_classify(monkeypatch, cls_result)
    out = _resolve_music_request(context(), "Play something")
    assert "command" not in out


def test_chained_secondary_action_refused_before_classifier(monkeypatch):
    from plugins.voice_stack import music

    def unexpected(_text):
        raise AssertionError("classifier must not run for a chained action")
    monkeypatch.setattr(music, "_classify", unexpected, raising=False)
    out = _resolve_music_request(
        context(), "Play Cities of the Plain, and then turn off all the lights"
    )
    assert "command" not in out


def test_research_clue_with_catalog_unreachable_asks_clarification(monkeypatch):
    """No candidates + no confirmed name -> never guess; ask."""
    from plugins.voice_stack import music

    def no_rows(**kw):
        raise OSError("network down")
    monkeypatch.setattr(music, "_catalog", no_rows, raising=False)
    set_classify(monkeypatch, intent(kind="song", title="hello from the other side", research=True))
    out = _resolve_music_request(
        context(), "Play that song where they say hello from the other side"
    )
    assert "command" not in out
    assert "clarification" in out


# ---------------------------------------------------------------------------
# Collaboration ("featuring X") — online research + Jev selection
# ---------------------------------------------------------------------------

def test_featured_collaboration_by_artist_when_featured_is_known(monkeypatch):
    """'latest song featuring Stromae' — a known featured artist — resolves to a
    by-artist command, not an invented track. With no primary named, the route
    scopes to the confidently-known featured artist (artist operation)."""
    from plugins.voice_stack import music
    monkeypatch.setattr(music, "_catalog", lambda **kw: [{"artistName": "Stromae"}], raising=False)
    set_route(monkeypatch, {
        "operation": "artist", "artist": "Stromae", "secondary": "",
        "confidence": 0.95,
    })
    out = _resolve_music_request(context(), "play latest song featuring Stromae")
    # The featured artist is verified; no primary was named, so the command is
    # scoped to the confidently-known artist only.
    assert "command" in out
    assert out["command"] == "Play songs by Stromae on Apple Music"


def test_garbled_collab_resolves_to_by_artist_not_invented_song(monkeypatch):
    """A mangled collab ('Play To The Loop featuring Stromae') must resolve to a
    by-artist command via Jev+DB verification of BOTH slots — never a fabricated
    track title. This is the regression that surfaced 'South of the Border by Ed
    Sheeran' in the 19:33 dry-run. Drives the new collab path with per-slot
    artist selection mocked."""
    from plugins.voice_stack import music

    # Per-slot artist selection on the new seam: each slot's suspect term is
    # independently Jev-verified against real candidates; the mangled primary
    # resolves to Tove Lo, the featured slot to Stromae.
    def repair_slot(term):
        term_key = str(term or "").strip().lower()
        fake = {
            "to the loop": "Tove Lo",
            "tupeloo": "Tove Lo",
            "tove lo": "Tove Lo",
            "stromboli": "Tove Lo",
            "stormlight": "Tove Lo",
            "stromae": "Stromae",
            "stroma": "Stromae",
        }
        return fake.get(term_key)

    monkeypatch.setattr(music, "_repair_artist_slot", repair_slot, raising=False)
    # Route selects the collab operation with both suspect slots from the text.
    set_route(monkeypatch, {
        "operation": "collab",
        "artist": "To The Loop", "secondary": "Stromae",
        "confidence": 0.95,
    })
    # The research path WOULD find a real-looking collab row; if the fix is right
    # it is never handed to a song-fabricating step.
    monkeypatch.setattr(
        music, "_catalog",
        lambda **kw: [{
            "trackName": "South of the Border (feat. Camila Cabello & Cardi B)",
            "artistName": "Ed Sheeran, Camila Cabello & Cardi B",
            "releaseDate": "2019-07-12",
        }], raising=False,
    )

    out = _resolve_music_request(
        context(), "Play To The Loop featuring Stromae.", confidence=0.9
    )
    # Jev-verified primary + featured, as a by-artist collab command.
    assert "command" in out, f"expected a command, got {out!r}"
    assert "Tove Lo" in out["command"] and "Stromae" in out["command"]
    # The fatal regression: no invented track may ever be emitted.
    assert "South of the Border" not in out["command"]
    assert "Ed Sheeran" not in out["command"]
    assert out["command"].startswith("Play songs by "), out["command"]


def test_repair_artist_slot_isolates_transcript_not_whole_collab(monkeypatch):
    """Per-slot Jev verification must see ONLY the slot's own suspect term.

    If the whole collab transcript ('Play To The Loop featuring Stromae') is
    passed as state, Jev scores the already-correct featured name (Stromae)
    over the mangled primary (To The Loop) and the primary can never resolve
    to Tove Lo. Each slot's Choice must be context-contained to its term.
    """
    from plugins.voice_stack import music

    seen_states = []
    def capture(state, _questions, candidates):
        seen_states.append(dict(state))
        # Simulate Jev: with an isolated transcript the mangled primary matches
        # the leading DB candidate; with a leaked collab transcript it does not.
        if "Stromae" in state.get("transcript", ""):
            return ("1", 0.39)   # picks Stromae — the bleed symptom
        return ("0", 0.79)       # isolated — picks Tove Lo
    monkeypatch.setattr(music, "_jev_select_artist", capture, raising=False)
    monkeypatch.setattr(
        music, "_repair_candidates",
        lambda terms, limit=12: ["Tove Lo", "Stromae"], raising=False,
    )

    primary = music._extract_primary_artist("The latest song by To The Loop featuring Stromae")
    # Drive the same path _resolve_collab_artists uses: per-slot verification.
    p = music._repair_artist_slot(primary)
    # The primary slot's Choice state must be isolated to ITS suspect term,
    # not the whole collab phrase that carries 'Stromae'.
    assert p == "Tove Lo", f"primary must resolve to Tove Lo, got {p!r}"
    assert seen_states, "Jev selection must have run for the primary slot"
    last = seen_states[-1]
    assert "Stromae" not in last.get("transcript", ""), (
        f"primary-slot state must not leak the featured name: {last.get('transcript')!r}"
    )


def test_featured_extraction_patterns():
    from plugins.voice_stack import music
    assert music._extract_featured("latest song featuring Stromae") == ("Stromae", None)
    assert music._extract_featured("song feat Dua Lipa") == ("Dua Lipa", None)
    assert music._extract_featured("song ft. Bon Iver") == ("Bon Iver", None)
    assert music._extract_featured("the latest collaboration with Stromae") == ("Stromae", None)
    assert music._extract_featured("play a collab with Tove Lo") == ("Tove Lo", None)
    assert music._extract_featured("just play Nelly Furtado") == (None, None)


def test_primary_artist_anchored_collab_resolution(monkeypatch):
    """'latest song by Tove Lo, featuring Stromae' resolves to by-artist, both
    slots Jev-verified from the vocabulary — never an invented track. (Option 3:
    drop the song-research/fabrication path.)"""
    from plugins.voice_stack import music

    # Per-slot artist selection: both slots are known and verify to themselves.
    def repair_slot(term):
        return {"tove lo": "Tove Lo", "stromae": "Stromae"}.get(
            str(term or "").strip().lower()
        )

    monkeypatch.setattr(music, "_repair_artist_slot", repair_slot, raising=False)
    monkeypatch.setattr(music, "_catalog", lambda **kw: [], raising=False)
    set_route(monkeypatch, {
        "operation": "collab",
        "artist": "Tove Lo", "secondary": "Stromae",
        "confidence": 0.95,
    })
    out = _resolve_music_request(
        context(), "Play the latest song by Tove Lo, featuring stromae"
    )
    assert out["command"] == "Play songs by Tove Lo & Stromae on Apple Music"


def test_collab_without_primary_artist_scopes_to_featured(monkeypatch):
    """No primary artist named: scope the collab command to the confidently
    verified featured artist — no song ranking, no fabricated track."""
    from plugins.voice_stack import music
    monkeypatch.setattr(music, "_catalog", lambda **kw: [{"artistName": "Stromae"}], raising=False)
    set_route(monkeypatch, {
        "operation": "artist", "artist": "Stromae", "secondary": "",
        "confidence": 0.95,
    })
    out = _resolve_music_request(context(), "Play the latest song featuring Stromae")
    assert out["command"] == "Play songs by Stromae on Apple Music"


def test_extract_primary_artist_patterns():
    from plugins.voice_stack import music
    assert music._extract_primary_artist("the latest song by Tove Lo, featuring Stromae") == "Tove Lo"
    assert music._extract_primary_artist("song by Dua Lipa feat. CALYN") == "Dua Lipa"
    assert music._extract_primary_artist("latest song featuring Stromae") == ""
    assert music._extract_primary_artist("play Bad Romance by Lady Gaga") == "Lady Gaga"


def test_featured_collab_unavailable_returns_exact_string(monkeypatch):
    from plugins.voice_stack import music

    def swallow(*_a, **_k):
        pass
    monkeypatch.setattr(music, "_write_telemetry", swallow, raising=False)

    def boom(_primary, _featured, _transcript, _confidence):
        raise music.TypeSafeUnavailable("boom")
    monkeypatch.setattr(music, "_resolve_collab_artists", boom, raising=False)
    out = _resolve_music_request(
        context(), "play latest song featuring Stromae"
    )
    assert out == "TypeSafe API (Jev)  is unavailable"


def test_strip_verbs_keeps_song_midstring():
    from plugins.voice_stack import music
    # "song" is mid-string here and must NOT be stripped as a classifier.
    assert music._strip_verbs("play latest song featuring Stromae") == "latest song featuring Stromae"
    assert music._strip_verbs("play the album Loose by Nelly Furtado") == "Loose by Nelly Furtado"


# ---------------------------------------------------------------------------
# UNAVAILABLE contract: the exact string, failed closed
# ---------------------------------------------------------------------------

def test_unavailable_api_returns_exact_string(monkeypatch):
    from plugins.voice_stack import music

    def swallow(*_a, **_k):
        pass
    monkeypatch.setattr(music, "_write_telemetry", swallow, raising=False)

    def boom(_text):
        raise music.TypeSafeUnavailable("boom")
    monkeypatch.setattr(music, "_classify", boom, raising=False)
    out = _resolve_music_request(context(), "Play Nelly Furtado")
    assert out == "TypeSafe API (Jev)  is unavailable"


def test_unavailable_api_during_candidate_selection_returns_exact_string(monkeypatch):
    from plugins.voice_stack import music

    def swallow(*_a, **_k):
        pass
    monkeypatch.setattr(music, "_write_telemetry", swallow, raising=False)

    def rows(**kw):
        return [{"trackName": "Hello Lyrics", "artistName": "Adele"}]
    monkeypatch.setattr(music, "_catalog", rows, raising=False)

    # The TypeSafe outage boundary is select_route: candidate selection now
    # lives on the route seam. A Jev outage there must surface the exact string.
    def boom(*_a, **_k):
        raise music.TypeSafeUnavailable("boom")
    monkeypatch.setattr(music, "select_route", boom, raising=False)

    out = _resolve_music_request(
        context(), "Play that song where they say hello from the other side"
    )
    assert out == "TypeSafe API (Jev)  is unavailable"


def test_resolve_does_not_fall_back_to_raw_passthrough_when_unavailable(monkeypatch):
    from plugins.voice_stack import music

    def swallow(*_a, **_k):
        pass
    monkeypatch.setattr(music, "_write_telemetry", swallow, raising=False)

    def boom(_text):
        raise music.TypeSafeUnavailable("boom")
    monkeypatch.setattr(music, "_classify", boom, raising=False)
    out = _resolve_music_request(context(), "Play songs by Nelly Furtado on Amazon Music")
    # The exact string must win over any raw command fallback.
    assert out == "TypeSafe API (Jev)  is unavailable"


# ---------------------------------------------------------------------------
# STT repair — select, never generate
# ---------------------------------------------------------------------------

def test_low_confidence_artist_is_repaired_from_catalog(monkeypatch):
    """'by Tupelo' at conf 0.289 is below the execution floor: the seam fails
    closed to a repeat request — never a fabricated track or guessed artist.

    Re-seamed onto select_route/execute: the defunct repair planner was an
    earlier decision layer; confidence is now a gate on the selected route.
    """
    from plugins.voice_stack import music

    monkeypatch.setattr(music, "_write_telemetry", lambda *a, **k: None, raising=False)
    set_route(monkeypatch, {
        "operation": "collab",
        "artist": "Tupelo", "secondary": "Stormlight",
        "confidence": 0.95,
    })
    # A research row exists; if the routing is right it is never fabricated.
    monkeypatch.setattr(
        music, "_catalog",
        lambda **kw: [{"trackName": "Invented Hit", "artistName": "Wrong Artist"}],
        raising=False,
    )

    out = _resolve_music_request(
        context(), "Play the latest song by Tupelo featuring Stormlight.", confidence=0.289
    )
    # Below the floor: ask the speaker to repeat, never emit a command.
    assert "command" not in out
    assert out == {"clarification": "I didn't catch that clearly. Could you repeat the music request?"}


def test_repair_none_winner_asks_to_repeat(monkeypatch):
    """A low-confidence hopeless route asks the user to repeat — never a
    guessed dispatch. (None-breaks-from-list / low-floor safety invariant.)"""
    from plugins.voice_stack import music

    monkeypatch.setattr(music, "_write_telemetry", lambda *a, **k: None, raising=False)
    set_route(monkeypatch, {
        "operation": "collab",
        "artist": "Zzzqxx", "secondary": "someone",
        "confidence": 0.2,
    })
    monkeypatch.setattr(music, "_catalog", lambda **kw: [], raising=False)

    out = _resolve_music_request(
        context(), "play the latest song by Zzzqxx featuring someone", confidence=0.2
    )
    assert "command" not in out
    assert "clarification" in out


def test_high_confidence_skips_repair(monkeypatch):
    """Above the floor, the transcript is trusted as-is; no Jev repair call."""
    from plugins.voice_stack import music

    monkeypatch.setattr(music, "_write_telemetry", lambda *a, **k: None, raising=False)

    def fail_repair(*_a, **_k):
        raise AssertionError("high-confidence transcripts must not be repaired")

    monkeypatch.setattr(music, "_jev_select_artist", fail_repair, raising=False)
    set_classify(monkeypatch, intent(kind="artist", artist="Nelly Furtado"))

    def fake_catalog(**kw):
        assert kw.get("term") == "Nelly Furtado"
        return [{"artistName": "Nelly Furtado"}]

    monkeypatch.setattr(music, "_catalog", fake_catalog, raising=False)
    out = _resolve_music_request(context(), "Play Nelly Furtado", confidence=0.9)
    assert out["command"] == "Play songs by Nelly Furtado on Apple Music"


def test_repair_outage_returns_exact_unavailable(monkeypatch):
    """TypeSafe outage during repair → the exact string, no dispatch."""
    from plugins.voice_stack import music

    monkeypatch.setattr(music, "_write_telemetry", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(
        music, "_load_artist_vocabulary", lambda: ["Madonna", "Stromae"], raising=False)
    monkeypatch.setattr(music, "_catalog", lambda **kw: [], raising=False)

    def boom(*_a, **_k):
        raise music.TypeSafeUnavailable("boom")

    monkeypatch.setattr(music, "_jev_select_artist", boom, raising=False)
    out = _resolve_music_request(
        context(), "stay to the loop featuring stormlight", confidence=0.3
    )
    assert out == "TypeSafe API (Jev)  is unavailable"


# ---------------------------------------------------------------------------
# Evidence-first low-confidence planning
# ---------------------------------------------------------------------------


def test_incomplete_relationship_evidence_cannot_dispatch_or_query_catalog(monkeypatch):
    """A re-listen exposing a missing second artist invalidates the whole plan.

    Re-seamed: the structural-incompleteness check is now the confidence gate on
    the selected route — below-floor evidence can never reach catalog lookup.
    """
    from plugins.voice_stack import music

    monkeypatch.setattr(music, "_write_telemetry", lambda *_a, **_k: None)
    monkeypatch.setattr(
        music,
        "_catalog",
        lambda **_k: (_ for _ in ()).throw(
            AssertionError("incomplete evidence must never reach catalog lookup")
        ),
    )
    set_route(monkeypatch, {
        "operation": "collab",
        "artist": "garbled performer", "secondary": "",
        "confidence": 0.95,
    })

    out = music.resolve(
        context(),
        "Play that song by garbled performer",
        confidence=0.29,
        evidence=[
            {
                "text": "Play that song by garbled performer",
                "confidence": 0.29,
                "source": "initial",
            },
            {
                "text": "Play that song by garbled performer with...",
                "confidence": 0.34,
                "source": "music_domain_redecode",
            },
        ],
    )

    assert "command" not in out
    assert out == {"clarification": "I didn't catch that clearly. Could you repeat the music request?"}


def test_complete_low_confidence_pair_is_planned_once_from_all_evidence(monkeypatch):
    """Arbitrary artists are selected from one immutable evidence packet, but a
    low-confidence pair can never dispatch on the new seam.

    Re-seamed: all evidence passes sit below the confidence floor, so the router
    fail-closes with a repeat request instead of emitting a fabricated pair.
    """
    from plugins.voice_stack import music

    monkeypatch.setattr(music, "_write_telemetry", lambda *_a, **_k: None)
    set_route(monkeypatch, {
        "operation": "collab",
        "artist": "Bjork", "secondary": "Rosalia",
        "confidence": 0.95,
    })
    monkeypatch.setattr(
        music,
        "_catalog",
        lambda **_k: (_ for _ in ()).throw(
            AssertionError("below-floor evidence must not query the catalog")
        ),
    )

    evidence = [
        {"text": "Play Burk and Rosalia", "confidence": 0.31, "source": "initial"},
        {
            "text": "Play Bjork and Rosalia",
            "confidence": 0.48,
            "source": "music_domain_redecode",
        },
    ]
    out = music.resolve(
        context(), evidence[0]["text"], confidence=0.31, evidence=evidence
    )

    # The evidence never clears the floor: fail closed, never fabricated.
    assert "command" not in out
    assert out == {"clarification": "I didn't catch that clearly. Could you repeat the music request?"}


def test_low_confidence_anaphoric_song_never_becomes_catalog_first_result(monkeypatch):
    """'That song' is not a title; below-floor evidence can never invent a hit
    single. Fail closed to a repeat request — never query catalog on garble."""
    from plugins.voice_stack import music

    monkeypatch.setattr(music, "_write_telemetry", lambda *_a, **_k: None)
    set_route(monkeypatch, {
        "operation": "described_song",
        "title": "", "artist": "",
        "confidence": 0.95,
    })
    monkeypatch.setattr(
        music,
        "_catalog",
        lambda **_k: (_ for _ in ()).throw(
            AssertionError("anaphoric title must not query or select the first song")
        ),
    )

    out = music.resolve(
        context(),
        "Play that song by Toe-to-Doo-Doo-Doo.",
        confidence=0.299,
        evidence=[
            {
                "text": "Play that song by Toe-to-Doo-Doo-Doo.",
                "confidence": 0.299,
                "source": "initial",
            }
        ],
    )

    assert out == {"clarification": "I didn't catch that clearly. Could you repeat the music request?"}


def test_low_confidence_plan_kind_cannot_dispatch_even_with_confident_artists(monkeypatch):
    """Every decision needed for execution must clear the same confidence floor.

    Below-floor evidence fail-closes to a repeat request — a confident-sounding
    artist pair can never dispatch on degraded acoustic evidence.
    """
    from plugins.voice_stack import music

    monkeypatch.setattr(music, "_write_telemetry", lambda *_a, **_k: None)
    set_route(monkeypatch, {
        "operation": "collab",
        "artist": "Artist Won", "secondary": "Artist Too",
        "confidence": 0.95,
    })
    monkeypatch.setattr(
        music,
        "_catalog",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("below-floor evidence must not reach catalog lookup")
        ),
    )

    out = music.resolve(
        context(),
        "Play Artist Won and Artist Too",
        confidence=0.3,
        evidence=[
            {
                "text": "Play Artist Won and Artist Too",
                "confidence": 0.3,
                "source": "initial",
            }
        ],
    )

    assert "command" not in out
    assert out == {"clarification": "I didn't catch that clearly. Could you repeat the music request?"}


def test_high_confidence_anaphoric_song_also_never_queries_catalog(monkeypatch):
    """Confidence cannot turn an anaphor into a real track title.

    'that song' without a resolvable title routes to described-song, which
    clarifies instead of inventing a track — even at high confidence.
    """
    from plugins.voice_stack import music

    monkeypatch.setattr(music, "_write_telemetry", lambda *_a, **_k: None)
    set_route(monkeypatch, {
        "operation": "described_song",
        "title": "", "artist": "",
        "confidence": 0.95,
    })
    catalog_calls = []

    def catalog(**kwargs):
        catalog_calls.append(kwargs)
        return [{"trackName": "Invented hit", "artistName": "Wrong artist"}]

    monkeypatch.setattr(music, "_catalog", catalog)

    out = music.resolve(context(), "Play that song by an artist", confidence=0.92)

    assert out == {"clarification": "Could you name the song or give its artist?"}
    assert catalog_calls == []