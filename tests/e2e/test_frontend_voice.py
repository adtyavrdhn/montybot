"""Voice in the web app (`sammy/static/voice.js`), with local API responses: dictating a message with the browser's
speech recognition (as on an iPhone) or through the server, and hearing replies in the server's voice or the
device's. The browser's microphone, speech recognition and sound are stand-ins that record what the page asks of them.
"""

from __future__ import annotations

import test_frontend
from playwright.sync_api import Page, ViewportSize, expect
from test_frontend import ASK, THREAD, MockAPI, emit, streaming_chat, workspace

frontend = test_frontend.frontend  # the same fixture: pytest finds a fixture by the name it has here

PHONE: ViewportSize = {'width': 390, 'height': 844}

SOUND = """
    window.played = [];
    HTMLMediaElement.prototype.play = function () { window.played.push(this.src); return Promise.resolve(); };
    HTMLMediaElement.prototype.pause = function () { window.paused = (window.paused || 0) + 1; };
    window.spoken = [];
    Object.defineProperty(window, 'speechSynthesis', { configurable: true, value: {
        speak(utterance) { window.spoken.push(utterance.text); window.utterance = utterance; },
        cancel() { window.cancelled = (window.cancelled || 0) + 1; },
    } });
"""
RECOGNITION = """
    window.recognitions = [];
    window.SpeechRecognition = undefined;
    window.webkitSpeechRecognition = class {
        constructor() { window.recognitions.push(this); }
        start() { this.started = true; }
        stop() { this.onend(); }
        abort() { this.aborted = true; this.onend(); }
    };
"""
RECORDER = """
    window.SpeechRecognition = undefined;
    window.webkitSpeechRecognition = undefined;
    window.micOff = 0;
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {
        getUserMedia: async () => ({ getTracks: () => [{ stop() { window.micOff += 1; } }] }),
    } });
    window.MediaRecorder = class {
        constructor() { this.mimeType = 'audio/mp4'; }
        start() {}
        stop() {
            this.ondataavailable({ data: new Blob(['recorded speech'], { type: 'audio/mp4' }) });
            this.onstop();
        }
    };
"""


def hear(page: Page, *words: str, recognition: int = 0) -> None:
    """The browser's speech recognition hears `words`, each a result of the session so far."""
    page.evaluate(
        '([index, words]) => window.recognitions[index].onresult({results: words.map((w) => [{transcript: w}])})',
        [recognition, list(words)],
    )


def sent(mock: MockAPI) -> list[str]:
    return [str(body['text']) for method, path, body in mock.calls if method == 'POST' and isinstance(body, dict)
            and path.startswith('/api/threads')]  # fmt: skip


def test_a_message_is_dictated_on_a_phone_and_sent(frontend: tuple[Page, MockAPI]) -> None:
    page, mock = frontend
    page.set_viewport_size(PHONE)
    page.add_init_script(SOUND + RECOGNITION)
    workspace(page, mock)
    message = page.locator('#message')
    message.fill('Please')
    page.get_by_role('button', name='Dictate', exact=True).click()
    stop = page.get_by_role('button', name='Stop dictating')
    expect(stop).to_have_attribute('aria-pressed', 'true')
    hear(page, 'add eggs')
    expect(message).to_have_value('Please add eggs')  # the words show as they are heard
    hear(page, 'add eggs', ' and milk')
    expect(message).to_have_value('Please add eggs and milk')
    stop.click()
    dictate = page.get_by_role('button', name='Dictate', exact=True)
    expect(dictate).to_have_attribute('aria-pressed', 'false')
    expect(message).to_be_focused()
    page.get_by_role('button', name='Send').click()
    expect(page.locator('.msg.user')).to_have_text('Please add eggs and milk')
    assert sent(mock) == ['Please add eggs and milk']

    # Sending while still dictating ends it, so later words do not fill the emptied box again.
    page.evaluate("location.hash = '#/new'")
    expect(page.locator('.msg.user')).to_have_count(0)
    dictate.click()
    hear(page, 'compare flights', recognition=1)
    expect(message).to_have_value('compare flights')
    page.get_by_role('button', name='Send').click()
    expect(dictate).to_have_attribute('aria-pressed', 'false')
    assert page.evaluate('window.recognitions[1].aborted')
    page.wait_for_function('document.getElementById("message").value === ""')
    assert sent(mock) == ['Please add eggs and milk', 'compare flights']


def test_without_speech_recognition_the_server_turns_a_recording_into_words(frontend: tuple[Page, MockAPI]) -> None:
    page, mock = frontend
    page.set_viewport_size(PHONE)
    mock.voice = {'transcribe': True, 'speak': False}
    page.add_init_script(SOUND + RECORDER)
    workspace(page, mock)
    page.locator('#message').fill('Please')
    page.get_by_role('button', name='Dictate', exact=True).click()
    page.get_by_role('button', name='Stop dictating').click()
    expect(page.locator('#message')).to_have_value('Please add eggs and milk')
    assert mock.transcribed == [('audio/mp4', b'recorded speech')]
    assert page.evaluate('window.micOff') == 1  # the microphone is let go
    page.get_by_role('button', name='Send').click()
    expect(page.locator('.msg.user')).to_have_text('Please add eggs and milk')


def test_no_microphone_where_neither_the_browser_nor_the_server_can_listen(frontend: tuple[Page, MockAPI]) -> None:
    page, mock = frontend
    page.add_init_script(SOUND + RECORDER)
    workspace(page, mock)
    page.wait_for_function('!document.getElementById("auto-read").hidden')  # the voice settings have arrived
    expect(page.locator('#dictate')).to_be_hidden()


def reply_chat(page: Page, mock: MockAPI) -> None:
    mock.signed_in = True
    mock.messages = [
        {'role': 'user', 'text': 'Compare flights'},
        {
            'role': 'assistant',
            'text': '**Three** options:\n\n- TAP at $120\n- [Ryanair](https://ryanair.example) at $90',
        },
    ]
    page.goto(f'http://sammy.test/#/t/{THREAD}')
    expect(page.locator('.msg.assistant')).to_be_visible()


SPOKEN = 'Three options:\nTAP at $120\nRyanair at $90'  # as the reply reads, without Markdown's marks


def test_a_reply_is_read_aloud_in_the_servers_voice(frontend: tuple[Page, MockAPI]) -> None:
    page, mock = frontend
    page.set_viewport_size(PHONE)
    mock.voice = {'transcribe': False, 'speak': True}
    page.add_init_script(SOUND)
    reply_chat(page, mock)
    page.get_by_role('button', name='Read aloud').click()
    expect(page.get_by_role('button', name='Stop reading')).to_have_attribute('aria-pressed', 'true')
    page.wait_for_function('window.played.length === 2')
    played = page.evaluate('window.played')
    assert played[0].startswith('data:audio/wav') and played[1].startswith('blob:')  # sound started in the tap
    assert mock.spoken == [SPOKEN]
    page.evaluate('voice.audio.onended()')
    expect(page.get_by_role('button', name='Read aloud')).to_have_attribute('aria-pressed', 'false')

    page.get_by_role('button', name='Read aloud').click()
    page.get_by_role('button', name='Stop reading').click()
    expect(page.get_by_role('button', name='Read aloud')).to_be_visible()

    mock.speech_status = 502
    page.get_by_role('button', name='Read aloud').click()
    expect(page.locator('#notice-text')).to_have_text('Speech is not working right now.')
    expect(page.get_by_role('button', name='Read aloud')).to_have_attribute('aria-pressed', 'false')


def test_without_a_server_voice_a_reply_is_read_in_the_devices_own(frontend: tuple[Page, MockAPI]) -> None:
    page, mock = frontend
    page.add_init_script(SOUND)
    reply_chat(page, mock)
    page.get_by_role('button', name='Read aloud').click()
    page.wait_for_function('(text) => window.spoken.includes(text)', arg=SPOKEN)
    assert not mock.spoken  # nothing went to the server
    page.evaluate('window.utterance.onend()')
    expect(page.get_by_role('button', name='Read aloud')).to_have_attribute('aria-pressed', 'false')
    page.get_by_role('button', name='Read aloud').click()
    page.get_by_role('button', name='Stop reading').click()
    assert page.evaluate('window.cancelled') >= 1


def test_read_replies_aloud_reads_a_reply_once_its_task_is_done(frontend: tuple[Page, MockAPI]) -> None:
    page, mock = frontend
    mock.voice = {'transcribe': False, 'speak': True}
    page.add_init_script(SOUND)
    streaming_chat(page, mock)
    toggle = page.locator('#auto-read')
    page.get_by_role('button', name='Read replies aloud').click()
    expect(toggle).to_have_attribute('aria-pressed', 'true')
    expect(toggle).to_have_text('Reading replies aloud')
    mock.messages.append({'role': 'assistant', 'text': 'Committed flight options'})
    mock.run = {'id': 'run', 'thread_id': THREAD, 'status': 'done', 'activity': [], 'ask': None}
    emit(page, 'status', mock.run)
    expect(page.get_by_role('button', name='Stop reading')).to_be_visible()
    page.wait_for_function('window.played.length === 2')  # sound started by the tap on the setting, then the reply
    assert mock.spoken == ['Committed flight options']

    page.reload()  # the setting stays; a reply already there is not read again
    expect(toggle).to_have_attribute('aria-pressed', 'true')
    expect(page.get_by_role('button', name='Read aloud')).to_be_visible()
    assert mock.spoken == ['Committed flight options']
    toggle.click()
    expect(toggle).to_have_attribute('aria-pressed', 'false')


def test_read_replies_aloud_reads_a_question_sammy_asks_while_you_watch(frontend: tuple[Page, MockAPI]) -> None:
    page, mock = frontend
    mock.voice = {'transcribe': False, 'speak': True}
    page.add_init_script(SOUND)
    streaming_chat(page, mock)
    page.get_by_role('button', name='Read replies aloud').click()
    question = 'Which date works for you, Friday or Saturday?'
    mock.run = {'id': 'run', 'thread_id': THREAD, 'status': 'waiting', 'activity': [],
                'ask': {'id': ASK, 'kind': 'question', 'prompt': question}}  # fmt: skip
    emit(page, 'status', mock.run)
    expect(page.locator('#ask')).to_contain_text(question)
    page.wait_for_function('window.played.length === 2')  # sound started by the tap on the setting, then the question
    assert mock.spoken == [question]

    emit(page, 'status', mock.run)  # the stream says it again: the same question is not read twice
    mock.messages.extend([{'role': 'assistant', 'text': question}, {'role': 'user', 'text': 'Friday'},
                          {'role': 'assistant', 'text': 'Booked for Friday.'}])  # fmt: skip
    mock.run = {'id': 'run', 'thread_id': THREAD, 'status': 'done', 'activity': [], 'ask': None}
    emit(page, 'status', mock.run)
    page.wait_for_function('window.played.length === 3')
    assert mock.spoken == [question, 'Booked for Friday.']

    # A chat opened while its question is already waiting does not read it out.
    mock.run = {'id': 'run', 'thread_id': THREAD, 'status': 'waiting', 'activity': [],
                'ask': {'id': ASK, 'kind': 'question', 'prompt': question}}  # fmt: skip
    page.reload()
    expect(page.locator('#ask')).to_contain_text(question)
    page.wait_for_function('window.eventSources.length === 1')
    emit(page, 'status', mock.run)
    page.get_by_role('button', name='Read aloud').last.click()  # a tap: whatever is read now comes after
    page.wait_for_function('window.played.length === 2')  # this page's silent start in the tap, then the reply
    assert mock.spoken == [question, 'Booked for Friday.', 'Booked for Friday.']
