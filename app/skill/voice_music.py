"""Spoken music selection and queue controls using Music Assistant's APIs."""

import asyncio
import itertools
import logging
import re
import secrets
import threading
import time
import unicodedata
from contextlib import asynccontextmanager
from xml.sax.saxutils import escape

import aiohttp
from ask_sdk_core.dispatch_components import AbstractRequestHandler
from env_secrets import get_env_secret
from music_assistant_client import MusicAssistantClient
from music_assistant_client.exceptions import InvalidServerVersion, TransportError
from music_assistant_models.enums import MediaType, QueueOption
from music_assistant_models.errors import (
    AuthenticationFailed,
    AuthenticationRequired,
    InvalidToken,
    MediaNotFoundError,
    MusicAssistantError,
)

from . import device_mapping, playback_requests

logger = logging.getLogger(__name__)
# Alexa speaks about 14 characters a second. Music Assistant's command that starts the new
# stream takes about as long to reach the Echo as Alexa takes to start speaking, so waiting
# for the speaking time alone lets the answer finish before the music cuts in.
SPOKEN_CHARACTERS_PER_SECOND = 14

# Favorite/library changes run after the answer, one at a time; the latest request for
# a song decides its state, even when an earlier change has not run yet.
_song_changes_lock = threading.Lock()
_song_change_order = threading.Lock()
_latest_song_changes = {}
_song_change_requests = itertools.count()

# A redelivered "play next" request must not queue the song twice.
PLAY_NEXT_REPEAT_SECONDS = 300
_play_next_lock = threading.Lock()
_recent_play_next = {}


@asynccontextmanager
async def connection():
    server = get_env_secret("MA_API_URL")
    token = get_env_secret("MA_API_TOKEN")
    if not server or not token:
        raise ValueError("Please connect the Music Assistant API token first.")
    async with (
        aiohttp.ClientSession() as session,
        MusicAssistantClient(server, session, token=token) as client,
    ):
        yield client


def normalized(value):
    return "".join(
        c for c in unicodedata.normalize("NFKD", value.casefold()) if c.isalnum()
    )


def base_title(name):
    """A track title without its trailing (Remastered) or [remix] qualifier."""
    bracket = min(
        (index for index in (name.find("("), name.find("[")) if index != -1),
        default=-1,
    )
    return name[:bracket].rstrip() if bracket != -1 else name


def shuffle(items):
    """Uniform random order for playback, drawn from the OS entropy source."""
    for end in range(len(items) - 1, 0, -1):
        swap = secrets.randbelow(end + 1)
        items[end], items[swap] = items[swap], items[end]


def choose(items):
    """One random item, drawn from the OS entropy source."""
    return items[secrets.randbelow(len(items))]


def choose_playlist(items, query):
    matches = [item for item in items if normalized(item.name) == normalized(query)]
    if len(matches) == 1:
        return matches[0]
    if len(items) == 1:
        return items[0]
    raise ValueError("I could not identify one playlist. Please use its full name.")


def title_and_artist(query):
    # SearchQuery cannot share an Alexa sample with another slot. Keep the
    # unrestricted query and interpret its final "by artist" qualifier here.
    matches = list(re.finditer(r"\s+by\s+", query, re.IGNORECASE))
    if matches:
        match = matches[-1]
        title, artist = query[: match.start()].strip(), query[match.end() :].strip()
        if title and artist:
            return title, artist
    return query.strip(), None


def artist_matches(item, requested):
    def key(name):
        return normalized(re.sub(r"^the\s+", "", name, flags=re.IGNORECASE))

    return any(
        key(artist.name) == key(requested) for artist in getattr(item, "artists", [])
    )


async def find_music(client, query, kind):
    title, artist = (
        title_and_artist(query) if kind in ("track", "album") else (query, None)
    )
    searches = [query] if not artist else [query, title]
    candidates = []
    for search in searches:
        results = await client.music.search(
            search, media_types=[MediaType(kind)], limit=25
        )
        items = getattr(
            results, {"track": "tracks", "artist": "artists", "album": "albums"}[kind]
        )
        candidates.extend(item for item in items if getattr(item, "available", True))
        if artist:
            # Titles such as Stand by Me contain 'by' without an artist qualifier.
            exact_phrase = [
                item
                for item in candidates
                if normalized(item.name) == normalized(query)
            ]
            if exact_phrase:
                return exact_phrase[0]
            matches = [
                item
                for item in candidates
                if artist_matches(item, artist)
                and normalized(base_title(item.name)) == normalized(title)
            ]
            if matches:
                return min(
                    matches, key=lambda item: normalized(item.name) != normalized(title)
                )
        elif candidates:
            return min(
                candidates, key=lambda item: normalized(item.name) != normalized(title)
            )
    raise ValueError(
        "I could not find that " + kind + (" by that artist." if artist else ".")
    )


async def active_queue(client, player_id):
    queue = await client.player_queues.get_active_queue(player_id)
    if queue is None:
        raise ValueError("This Echo does not have a Music Assistant queue yet.")
    return queue


def artist_names(item):
    return ", ".join(a.name for a in getattr(item, "artists", None) or [] if a.name)


def announce(kind, item):
    """Say what was found, so a different match than intended is noticed right away."""
    artists = artist_names(item)
    by = " by " + artists if artists else ""
    return {
        "track": "Playing " + item.name + by,
        "album": "Playing the album " + item.name + by,
        "artist": "Playing songs by " + item.name,
        "playlist": "Playing your playlist " + item.name,
    }[kind] + "."


async def prepare_music(player_id, query="", kind="track", recommended=False):
    """Resolve without changing playback, while Alexa can report search errors."""
    async with connection() as client:
        queue = await active_queue(client, player_id)
        if recommended:
            media, label = await recommended_seed(client)
            name = label or "your music"
            announcement = (
                "Playing " + label + "." if label else "Playing your recommendations."
            )
        elif kind == "favorites":
            media = await favorite_tracks(client)
            name = "your favorites"
            announcement = "Playing your favorites."
        else:
            if kind == "playlist":
                playlists = await client.music.get_library_playlists(
                    search=query, limit=20
                )
                item = choose_playlist(
                    [item for item in playlists if getattr(item, "available", True)],
                    query,
                )
            else:
                item = await find_music(client, query, kind)
            name = item.name
            media = item.uri
            announcement = announce(kind, item)
            if kind == "artist":
                tracks = await client.music.get_artist_tracks(
                    item.item_id, item.provider
                )
                media = [
                    track.uri for track in tracks if getattr(track, "available", True)
                ]
                if not media:
                    raise ValueError("I could not find tracks for that artist.")
        return {
            "queue_id": queue.queue_id,
            "media": media,
            "name": name,
            "announcement": announcement,
            "radio": recommended or kind == "track",
            "recommended": recommended,
        }


async def play_without_radio(client, queue_id, media, option):
    # Explicit False prevents inherited shuffle on finite selections.
    await playback_requests.write(
        client.send_command,
        "player_queues/play_media",
        queue_id=queue_id,
        media=media,
        option=option,
        radio_mode=False,
        shuffle=False,
    )


async def start_mix(client, queue_id, seed):
    """Replace the upcoming tracks with an endless mix based on the seed song."""
    await playback_requests.write(
        client.player_queues.play_media,
        queue_id,
        seed,
        option=QueueOption.REPLACE_NEXT,
        radio_mode=True,
    )


async def play_music_selection(selection):
    """Start already resolved music without repeating the search."""
    queue_id, media = selection["queue_id"], selection["media"]
    async with connection() as client:
        await play_without_radio(client, queue_id, media, QueueOption.REPLACE)
        if not selection["radio"]:
            return
        # A dynamic mix can choose any of its tracks first, even with a
        # start_item. Start the selected song literally, then replace only
        # the upcoming tail with its endless mix, as "play similar" does.
        try:
            return await start_mix(client, queue_id, media)
        except MusicAssistantError as exc:
            logger.warning(
                "Similar mix unavailable (%s); selected song keeps playing",
                type(exc).__name__,
            )
        if selection["recommended"]:
            tracks = [uri for uri in await library_selection(client) if uri != media]
            if tracks:
                await play_without_radio(
                    client, queue_id, tracks, QueueOption.REPLACE_NEXT
                )


async def select_and_play(player_id, query, playlist=False, kind="track"):
    selection = await prepare_music(player_id, query, "playlist" if playlist else kind)
    await play_music_selection(selection)
    return selection["name"]


async def play_next(player_id, query):
    """Insert one song after the current one; a running mix continues after it."""
    async with connection() as client:
        queue = await active_queue(client, player_id)
        if getattr(queue, "current_item", None) is None:
            raise ValueError("Nothing is playing. Ask me to play the song instead.")
        track = await find_music(client, query, "track")
        await client.player_queues.play_media(
            queue.queue_id, track.uri, option=QueueOption.NEXT
        )
        return track.name


async def favorite_tracks(client):
    tracks = await client.music.get_library_tracks(favorite=True, limit=500)
    uris = [track.uri for track in tracks if getattr(track, "available", True)]
    if not uris:
        raise ValueError(
            "You have no favorite songs yet. Say add this song to my favorites to add one."
        )
    shuffle(uris)
    return uris


async def library_copy(client, track):
    if track.provider == "library":
        return track
    try:
        return await client.music.get_library_item_by_prov_id(
            MediaType.TRACK, track.item_id, track.provider
        )
    except MediaNotFoundError:
        return None


def in_collection(saved, collection):
    return saved is not None and (
        collection == "library" or getattr(saved, "favorite", False)
    )


async def apply_song_change(client, collection, add, track, saved):
    if collection == "favorites" and add:
        await client.music.add_item_to_favorites(track.uri)
    elif collection == "favorites":
        await client.music.remove_item_from_favorites(MediaType.TRACK, saved.item_id)
    elif add:
        await client.music.add_item_to_library(track.uri)
    else:
        await client.music.remove_item_from_library(MediaType.TRACK, saved.item_id)


async def plan_song_change(player_id, collection, add):
    """Check the current song now and return (message, change or None).

    Saving to the library also runs Music Assistant's full metadata scan before
    it answers, which can outlast Alexa's response deadline, so the change itself
    runs after the response.
    """
    async with connection() as client:
        queue = await active_queue(client, player_id)
        track = getattr(getattr(queue, "current_item", None), "media_item", None)
        if track is None or track.media_type != MediaType.TRACK:
            raise ValueError("Please play a song first.")
        saved = await library_copy(client, track)
    key = (collection, track.uri)
    with _song_changes_lock:
        latest = _latest_song_changes.get(key)
        # A change that has not run yet decides what the song will be.
        present = latest[0] if latest else in_collection(saved, collection)
        if add == present:
            state = "already" if add else "not"
            return f"{track.name} is {state} in your {collection}.", None
        request = next(_song_change_requests)
        _latest_song_changes[key] = (add, request)

    def is_latest():
        with _song_changes_lock:
            return _latest_song_changes.get(key, (None, None))[1] == request

    async def change():
        try:
            with _song_change_order:
                if not is_latest():
                    return  # a later request for this song decides
                async with connection() as client:
                    saved = await library_copy(client, track)
                    if add == in_collection(saved, collection):
                        return
                    await apply_song_change(client, collection, add, track, saved)
        finally:
            with _song_changes_lock:
                if _latest_song_changes.get(key, (None, None))[1] == request:
                    del _latest_song_changes[key]

    action = "Adding {} to" if add else "Removing {} from"
    return action.format(track.name) + f" your {collection}.", change


async def library_tracks(client):
    """Random available favorites, or else random library tracks."""
    for favorite in (True, None):
        tracks = await client.music.get_library_tracks(
            favorite=favorite, limit=50, order_by="random"
        )
        tracks = [track for track in tracks if getattr(track, "available", True)]
        if tracks:
            shuffle(tracks)
            return tracks
    return []


async def library_selection(client):
    return [track.uri for track in await library_tracks(client)]


def recommendation_identities(track):
    """Link the same song across rows and MA/provider representations."""
    identities = {("uri", track["uri"])}
    for mapping in track.get("provider_mappings", []):
        for provider in (
            mapping.get("provider_instance"),
            mapping.get("provider_domain"),
        ):
            if provider and mapping.get("item_id"):
                identities.add(("uri", f"{provider}://track/{mapping['item_id']}"))
    for kind, value in track.get("external_ids", []):
        if str(kind).lower() == "isrc" and value:
            identities.add(("isrc", value.upper()))
    artists = tuple(
        sorted(
            normalized(artist["name"])
            for artist in track.get("artists", [])
            if artist.get("name")
        )
    )
    if track.get("name") and artists:
        identities.add(
            (
                "song",
                normalized(track["name"]),
                normalized(track.get("version") or ""),
                artists,
            )
        )
    return identities


def unique_recommendations(tracks):
    """Union overlapping identities before sampling, including transitive aliases."""
    parents = list(range(len(tracks)))
    aliases = {}

    def root(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for index, track in enumerate(tracks):
        for identity in recommendation_identities(track):
            if identity in aliases:
                parents[root(index)] = root(aliases[identity])
            aliases[identity] = index
    songs = {}
    for index, track in enumerate(tracks):
        songs.setdefault(root(index), track["uri"])
    return list(songs.values())


def song_label(name, artists):
    """'Song by Artist', just the song without artists, or None without a name."""
    if not name:
        return None
    return name + " by " + artists if artists else name


async def recommended_seed(client):
    """Uniform choice from all unique playable recommendation songs: (uri, label)."""
    rows = await client.music.recommendations()
    tracks = []
    # Without a background listener this short-lived MA client receives each
    # reply inline. Concurrent commands would read the same WebSocket at once.
    for row in rows:
        try:
            items = await client.send_command(
                "music/recommendations/items",
                provider=row.provider,
                item_id=row.item_id,
            )
            tracks.extend(
                item
                for item in items
                if item.get("media_type") == "track"
                and item.get("uri")
                and item.get("available", True)
            )
        except MusicAssistantError:
            logger.warning("Skipping unavailable recommendation row")
    pool = unique_recommendations(tracks)
    if pool:
        uri = choose(pool)
        track = next(track for track in tracks if track["uri"] == uri)
        artists = ", ".join(
            artist["name"] for artist in track.get("artists", []) if artist.get("name")
        )
        return uri, song_label(track.get("name"), artists)
    tracks = await library_tracks(client)
    if tracks:
        track = choose(tracks)
        return track.uri, song_label(getattr(track, "name", None), artist_names(track))
    raise ValueError(
        "Music Assistant has no recommended songs or library tracks available right now."
    )


async def play_recommended(player_id):
    selection = await prepare_music(player_id, recommended=True)
    await play_music_selection(selection)


def current_track(queue):
    media = getattr(getattr(queue, "current_item", None), "media_item", None)
    if media is None or media.media_type != MediaType.TRACK:
        raise ValueError("Please play a song first.")
    return media


async def check_similar(player_id):
    """Confirm a song is playing before promising a mix based on it."""
    async with connection() as client:
        current_track(await active_queue(client, player_id))


async def play_similar(player_id):
    async with connection() as client:
        queue = await active_queue(client, player_id)
        seed = current_track(queue).uri
        await start_mix(client, queue.queue_id, seed)


async def prepare_resume(player_id, refuse_playing=True):
    """Check the Echo's queue can be resumed; return its id and the song it continues."""
    async with connection() as client:
        queue = await active_queue(client, player_id)
    if not queue.items:
        raise ValueError(
            "There is nothing to resume. Ask me to play something instead."
        )
    if refuse_playing and getattr(queue, "state", None) == "playing":
        raise ValueError("Your music is already playing.")
    return queue.queue_id, queue.current_item


async def resume_queue(queue_id):
    """Continue the queue where it stopped, as the Music Assistant app's play button does."""
    async with connection() as client:
        await playback_requests.write(client.player_queues.resume, queue_id)


def current_station(queue):
    media = getattr(getattr(queue, "current_item", None), "media_item", None)
    return media if getattr(media, "media_type", None) == MediaType.RADIO else None


def station_matches(left, right):
    """Match library references and provider references without guessing by name."""

    def identities(item):
        keys = {(item.provider, item.item_id)}
        for mapping in getattr(item, "provider_mappings", []):
            keys.add((mapping.provider_instance, mapping.item_id))
            keys.add((mapping.provider_domain, mapping.item_id))
        return keys

    return bool(identities(left) & identities(right))


async def saved_stations(client):
    stations = []
    offset = 0
    while True:
        page = await client.music.get_library_radios(limit=500, offset=offset)
        stations.extend(item for item in page if getattr(item, "available", True))
        if len(page) < 500:
            break
        offset += len(page)
    stations.sort(key=lambda item: (item.name.casefold(), item.uri))
    if not stations:
        raise ValueError(
            "Please add some radio stations to your Music Assistant library first."
        )
    return stations


def named_station(stations, query):
    key = normalized(query)
    if not key:
        raise ValueError("Please say start followed by the station name.")
    matches = [item for item in stations if normalized(item.name) == key]
    if not matches:
        matches = [item for item in stations if key in normalized(item.name)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        names = ", ".join(item.name for item in matches[:3])
        raise ValueError(
            "I found multiple stations: "
            + names
            + ". Please say start followed by the full station name."
        )
    raise ValueError("I could not find that station in your Music Assistant library.")


async def station_for(client, stations, action, query, current, queue):
    """The saved station this request starts, after ambiguity and history checks."""
    if action == "named":
        return named_station(stations, query)
    if action in ("next", "previous"):
        index = next(
            (i for i, item in enumerate(stations) if station_matches(item, current)),
            None,
        )
        if index is None:
            raise ValueError(
                "The current station is not in your saved radio library. Please start a saved station first."
            )
        return stations[(index + (1 if action == "next" else -1)) % len(stations)]
    selected = next(
        (item for item in stations if current and station_matches(item, current)),
        None,
    )
    if selected is None:
        history = await client.music.recently_played(
            limit=50,
            media_types=[MediaType.RADIO],
            queue_id=queue.queue_id,
            fully_played_only=False,
        )
        selected = next(
            (
                station
                for previous in history
                for station in stations
                if station_matches(station, previous)
            ),
            None,
        )
    return selected if selected is not None else choose(stations)


def navigation_unchanged(latest, queue, expected):
    """True while the playback the navigation was planned against is still there."""
    return (
        latest.queue_id == queue.queue_id
        and current_station(latest) is not None
        and getattr(latest, "state", None) == "playing"
        and getattr(latest.current_item, "queue_item_id", None) == expected
    )


async def prepare_radio(player_id, action, query=""):
    """Resolve before acknowledging so ambiguity and inactive radio are spoken."""
    async with connection() as client:
        queue = await active_queue(client, player_id)
        current = current_station(queue)
        navigating = action in ("next", "previous")
        if navigating and (
            current is None or getattr(queue, "state", None) != "playing"
        ):
            raise ValueError("No radio station is playing. Your playback is unchanged.")
        stations = await saved_stations(client)
        selected = await station_for(client, stations, action, query, current, queue)
        # Navigation must not interrupt music started while this request was preparing.
        expected = (
            getattr(queue.current_item, "queue_item_id", None) if navigating else None
        )
        if navigating:
            latest = await active_queue(client, player_id)
            if not navigation_unchanged(latest, queue, expected):
                raise ValueError(
                    "Playback changed while selecting a station. Please try again."
                )
        return selected, queue.queue_id, expected


async def play_station(player_id, station, queue_id, expected_item=None):
    async with connection() as client:
        latest = await active_queue(client, player_id)
        if latest.queue_id != queue_id:
            return
        # Cancel navigation if another song or station replaced the original.
        latest_item = getattr(latest, "current_item", None)
        if expected_item is not None and (
            getattr(latest, "state", None) != "playing"
            or latest_item is None
            or current_station(latest) is None
            or latest_item.queue_item_id != expected_item
        ):
            logger.info("Radio navigation cancelled because playback changed")
            return
        await play_without_radio(client, queue_id, station.uri, QueueOption.REPLACE)


def describe_station(queue, include_song=False):
    station = current_station(queue)
    if station is None or getattr(queue, "state", None) not in ("playing", "paused"):
        return "No radio station is playing."
    message = "You are listening to " + station.name + "."
    if include_song:
        details = getattr(queue.current_item, "streamdetails", None)
        metadata = getattr(details, "stream_metadata", None)
        title = getattr(metadata, "title", None)
        artist = getattr(metadata, "artist", None)
        if title and normalized(title) != normalized(station.name):
            message += " This is " + title + (" by " + artist if artist else "") + "."
    return message


def describe_item(item):
    media = getattr(item, "media_item", None)
    title = getattr(media, "name", None) or getattr(item, "name", "an unknown track")
    artists = ", ".join(a.name for a in getattr(media, "artists", []))
    return title + (" by " + artists if artists else "")


async def queue_information(player_id, which):
    async with connection() as client:
        queue = await active_queue(client, player_id)
        if which == "current" and current_station(queue) is not None:
            return describe_station(queue, include_song=True)
        item, message = await queue_item_at(client, queue, which)
        if item is None:
            return message
        prefix = {
            "current": "This is ",
            "next": "Next is ",
            "previous": "Before this was ",
        }[which]
        return prefix + describe_item(item) + "."


async def queue_item_at(client, queue, which):
    """The item `which` points at, and the message when the queue has none."""
    if queue.current_index is None or queue.current_item is None:
        return None, "There is no current song."
    if which == "current":
        return queue.current_item, None
    if which == "next" and queue.next_item is not None:
        return queue.next_item, None
    index = queue.current_index + (1 if which == "next" else -1)
    if index < 0 or index >= queue.items:
        return None, "There is no " + which + " song in this queue."
    items = await client.player_queues.get_queue_items(
        queue.queue_id, limit=1, offset=index
    )
    item = items[0] if items else None
    if item is None:
        return None, "There is no " + which + " song in this queue."
    return item, None


def same_album(left, right):
    return (
        left is not None
        and right is not None
        and (
            left.uri == right.uri
            or (
                getattr(left, "name", None)
                and getattr(right, "name", None)
                and normalized(left.name) == normalized(right.name)
            )
        )
    )


def shuffle_source_error(queue, kind, current, sources, album):
    """Why this queue cannot be shuffled, or None when it can."""
    if (
        current is None
        or queue.current_index is None
        or queue.is_dynamic
        or len(sources) != 1
        or sources[0].media_type != MediaType(kind)
        or (kind == "album" and not same_album(sources[0], album))
    ):
        return (
            "Please start "
            + ("an album" if kind == "album" else "a playlist")
            + " first."
        )
    if queue.shuffle_enabled:
        return "This queue already has shuffle enabled. Please start it normally first."
    return None


async def unbuffered_items(client, queue, boundary):
    """The queue tail after boundary, fetched past Music Assistant's buffer."""
    items, offset = [], boundary + 1
    while offset < queue.items:
        page = await client.player_queues.get_queue_items(
            queue.queue_id, limit=500, offset=offset
        )
        if not page:
            break
        items.extend(page)
        offset += len(page)
    return items


def require_album_tail(items, album):
    if any(
        not same_album(getattr(getattr(item, "media_item", None), "album", None), album)
        for item in items
    ):
        raise ValueError(
            "The remaining queue contains other music. Please start an album first."
        )


async def reorder_shuffled(client, player_id, queue, current, order, original):
    """Apply the shuffled order, restoring the queue when a move fails."""
    ids = list(original)

    async def unchanged_playback():
        latest = await active_queue(client, player_id)
        if (
            latest.queue_id != queue.queue_id
            or latest.current_item is None
            or latest.current_item.queue_item_id != current.queue_item_id
            or latest.index_in_buffer != queue.index_in_buffer
        ):
            raise ValueError("Playback advanced while shuffling. Please try again.")

    try:
        await reorder(
            client,
            queue.queue_id,
            ids,
            [item.queue_item_id for item in order],
            unchanged_playback,
        )
    except Exception:
        # Moves can't be applied at once; put back the order the user had.
        try:
            await reorder(client, queue.queue_id, ids, original)
        except Exception:
            logger.exception("Could not restore the queue order after shuffling")
        raise


async def shuffle_selection(player_id, kind):
    """Reorder only a finite album/playlist tail; never enable shuffle mode."""
    async with connection() as client:
        queue = await active_queue(client, player_id)
        current = getattr(queue, "current_item", None)
        media = getattr(current, "media_item", None)
        album = getattr(media, "album", None)
        sources = getattr(queue, "sources", [])
        error = shuffle_source_error(queue, kind, current, sources, album)
        if error:
            raise ValueError(error)
        boundary = max(
            queue.current_index,
            queue.index_in_buffer if queue.index_in_buffer is not None else -1,
        )
        items = await unbuffered_items(client, queue, boundary)
        if len(items) < 2:
            raise ValueError("There are not enough unbuffered tracks left to shuffle.")
        if kind == "album":
            require_album_tail(items, album)
        order = list(items)
        shuffle(order)
        if order == items:
            order = order[1:] + order[:1]
        original = [item.queue_item_id for item in items]
        await reorder_shuffled(client, player_id, queue, current, order, original)


async def reorder(client, queue_id, ids, wanted, before_move=None):
    """Move queue items until ids (kept up to date) matches the wanted order."""
    for target, item_id in enumerate(wanted):
        if before_move:
            await before_move()
        position = ids.index(item_id)
        if position != target:
            await client.player_queues.move_item(
                queue_id, item_id, pos_shift=target - position
            )
            ids.insert(target, ids.pop(position))


async def shuffle_album(player_id):
    await shuffle_selection(player_id, "album")


async def shuffle_playlist(player_id):
    await shuffle_selection(player_id, "playlist")


MUSIC_INTENTS = {
    "PlaySongIntent": "track",
    "PlayPlaylistIntent": "playlist",
    "PlayArtistIntent": "artist",
    "PlayAlbumIntent": "album",
}
INFO_INTENTS = {
    "WhatsPlayingIntent": "current",
    "WhatsNextIntent": "next",
    "WhatsPreviousIntent": "previous",
}
SHUFFLE_INTENTS = {
    "ShuffleAlbumIntent": "album",
    "ShufflePlaylistIntent": "playlist",
}
SONG_INTENTS = {
    "AddFavoriteIntent": ("favorites", True),
    "RemoveFavoriteIntent": ("favorites", False),
    "AddToLibraryIntent": ("library", True),
    "RemoveFromLibraryIntent": ("library", False),
}
RADIO_INTENTS = {
    "StartRadioIntent": "resume",
    "StartStationIntent": "named",
    "NextStationIntent": "next",
    "PreviousStationIntent": "previous",
}
HANDLED_INTENTS = (
    MUSIC_INTENTS.keys()
    | INFO_INTENTS.keys()
    | SHUFFLE_INTENTS.keys()
    | SONG_INTENTS.keys()
    | RADIO_INTENTS.keys()
    | {
        "PlaySimilarIntent",
        "PlayRecommendedIntent",
        "PlayFavoritesIntent",
        "PlayNextIntent",
        "ResumeMusicIntent",
    }
)


def say(response, text):
    return response.speak(escape(text)).set_should_end_session(True).response


def run_now(work, seconds):
    """Run a short MA request inside Alexa's response deadline."""

    async def timed():
        async with asyncio.timeout(seconds):
            return await work()

    return asyncio.run(timed())


def music_assistant_configured():
    return bool(get_env_secret("MA_API_URL") and get_env_secret("MA_API_TOKEN"))


# MA can't be reached or won't let the skill in; the stored stream may still play.
MA_UNREACHABLE = (
    TransportError,
    InvalidServerVersion,
    AuthenticationRequired,
    AuthenticationFailed,
    InvalidToken,
    aiohttp.ClientError,
    OSError,
    TimeoutError,
)


def resume_for_alexa(response, player):
    """Alexa's own resume: continue the MA queue silently, with a fresh stream.

    The stream the Echo had before may be gone after a break, and MA's play button for
    this player also arrives here, while MA already reports it as playing. Returns
    None when MA can't be reached, so the caller can replay the stored stream instead.
    """
    try:
        queue_id, _current = run_now(
            lambda: prepare_resume(player, refuse_playing=False), 5
        )
    except ValueError as exc:
        return say(response, str(exc))
    except MA_UNREACHABLE:
        logger.warning("Music Assistant unreachable; resuming the stored stream")
        return None
    except Exception:
        logger.exception("Resume failed")
        return say(
            response, "I could not resume your music right now. Please try again."
        )
    run_after_response(
        player,
        "voice-playback",
        playback_requests.begin(player),
        lambda: resume_queue(queue_id),
    )
    return response.set_should_end_session(True).response


def repeated_play_next(request_id):
    """The song an already handled delivery of this request queued, if any."""
    with _play_next_lock:
        now = time.monotonic()
        for seen, (at, _song) in list(_recent_play_next.items()):
            if now - at > PLAY_NEXT_REPEAT_SECONDS:
                del _recent_play_next[seen]
        return request_id and _recent_play_next.get(request_id, (None, None))[1]


def remember_play_next(request_id, song):
    if request_id:
        with _play_next_lock:
            _recent_play_next[request_id] = (time.monotonic(), song)


def speaking_time(text):
    return len(text) / SPOKEN_CHARACTERS_PER_SECOND


def start_after_answer(player, response, message, work, name="voice-playback"):
    """Answer, then start playback once Alexa has said it; older pending starts end here."""
    ticket = playback_requests.begin(player)

    async def after_answer():
        await asyncio.sleep(speaking_time(message))
        await work()

    run_after_response(player, name, ticket, after_answer)
    return say(response, message)


def run_after_response(player, name, ticket, work):
    """Change playback in the background unless a newer command supersedes it."""

    def run():
        playback_requests.activate(ticket)
        try:
            run_now(work, 90)
            logger.info("%s finished for %s", name, player)
        except playback_requests.SupersededPlayback:
            logger.info("Cancelled superseded %s for %s", name, player)
        except Exception:
            logger.exception("Background %s failed for %s", name, player)

    threading.Thread(target=run, name=name, daemon=True).start()


class VoiceMusicHandler(AbstractRequestHandler):
    def can_handle(self, handler_input):
        intent = getattr(handler_input.request_envelope.request, "intent", None)
        return intent is not None and intent.name in HANDLED_INTENTS

    def handle(self, handler_input):
        """Resolve the request, then answer — one intent branch at a time."""
        name = handler_input.request_envelope.request.intent.name
        slot = (handler_input.request_envelope.request.intent.slots or {}).get("query")
        query = getattr(slot, "value", "") or ""
        device_id = handler_input.request_envelope.context.system.device.device_id
        player = device_mapping.get_player_for_device(device_id)
        response = handler_input.response_builder
        if not player:
            return say(
                response,
                "Please map this Echo to its player in the bridge devices page first.",
            )
        if name in RADIO_INTENTS:
            return self._start_radio(response, player, name, query)
        if (name in MUSIC_INTENTS or name == "PlayNextIntent") and not query.strip():
            return say(response, "Please say a song, artist, album or playlist name.")
        if name == "PlayNextIntent":
            return self._play_next(response, player, handler_input, query)
        if name in SONG_INTENTS:
            return self._change_song(response, player, name)
        if name in INFO_INTENTS or name in SHUFFLE_INTENTS:
            return self._queue_request(response, player, name)
        if name == "ResumeMusicIntent":
            return self._resume(response, player)
        if name == "PlaySimilarIntent":
            return self._similar(response, player)
        return self._play_music(response, player, name, query)

    def _start_radio(self, response, player, name, query):
        try:
            station, queue_id, expected = run_now(
                lambda: prepare_radio(player, RADIO_INTENTS[name], query), 5
            )
        except ValueError as exc:
            return say(response, str(exc))
        except Exception:
            logger.exception("Radio selection failed")
            return say(
                response,
                "I could not find a radio station right now. Please try again.",
            )

        return start_after_answer(
            player,
            response,
            "Starting " + station.name + ".",
            lambda: play_station(player, station, queue_id, expected),
            name="voice-radio",
        )

    def _play_next(self, response, player, handler_input, query):
        request_id = getattr(handler_input.request_envelope.request, "request_id", None)
        if song := repeated_play_next(request_id):
            return say(response, song + " will play next.")
        try:
            song = run_now(lambda: play_next(player, query), 6)
            remember_play_next(request_id, song)
        except ValueError as exc:
            return say(response, str(exc))
        except Exception:
            logger.exception("Play next failed")
            return say(
                response, "I could not queue that song right now. Please try again."
            )
        return say(response, song + " will play next.")

    def _change_song(self, response, player, name):
        try:
            message, change = run_now(
                lambda: plan_song_change(player, *SONG_INTENTS[name]), 5
            )
        except ValueError as exc:
            return say(response, str(exc))
        except Exception:
            logger.exception("Song update failed")
            return say(
                response,
                "I could not update that song right now. Please try again.",
            )
        if change is not None:
            run_after_response(player, "voice-song-update", None, change)
        return say(response, message)

    def _queue_request(self, response, player, name):
        async def queue_request():
            if name in INFO_INTENTS:
                return await queue_information(player, INFO_INTENTS[name])
            await shuffle_selection(player, SHUFFLE_INTENTS[name])
            return "The remaining " + SHUFFLE_INTENTS[name] + " tracks are shuffled."

        try:
            return say(response, run_now(queue_request, 5))
        except ValueError as exc:
            return say(response, str(exc))
        except Exception:
            logger.exception("Voice queue request failed")
            return say(
                response, "I could not finish that queue request. Please try again."
            )

    def _resume(self, response, player):
        try:
            queue_id, current = run_now(lambda: prepare_resume(player), 5)
        except ValueError as exc:
            return say(response, str(exc))
        except Exception:
            logger.exception("Resume failed")
            return say(
                response,
                "I could not resume your music right now. Please try again.",
            )

        what = describe_item(current) if current is not None else "your music"
        return start_after_answer(
            player,
            response,
            "Resuming " + what + ".",
            lambda: resume_queue(queue_id),
        )

    def _similar(self, response, player):
        try:
            run_now(lambda: check_similar(player), 5)
        except ValueError as exc:
            return say(response, str(exc))
        except Exception:
            logger.exception("Similar songs check failed")
            return say(
                response,
                "I could not start similar songs right now. Please try again.",
            )
        return start_after_answer(
            player,
            response,
            "Switching to similar songs after this track.",
            lambda: play_similar(player),
        )

    def _play_music(self, response, player, name, query):
        try:
            selection = run_now(
                lambda: prepare_music(
                    player,
                    query,
                    "favorites"
                    if name == "PlayFavoritesIntent"
                    else MUSIC_INTENTS.get(name, "track"),
                    recommended=name == "PlayRecommendedIntent",
                ),
                6,
            )
        except ValueError as exc:
            return say(response, str(exc))
        except TimeoutError:
            return say(
                response,
                "Music Assistant took too long to find that music. Please try again.",
            )
        except Exception:
            logger.exception("Music selection failed")
            return say(
                response, "I could not prepare that music right now. Please try again."
            )

        return start_after_answer(
            player,
            response,
            selection["announcement"],
            lambda: play_music_selection(selection),
        )
