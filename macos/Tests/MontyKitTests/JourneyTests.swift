import Foundation
import Testing
@testable import MontyKit

// A person using the app, through the same models the views use, against a real montybot with the scripted model
// and fixture sites: `uv run python macos/scripts/dev_server.py`, then `MONTY_TEST_SERVER=http://127.0.0.1:8000
// swift test`. Without MONTY_TEST_SERVER these are skipped.

let server = ProcessInfo.processInfo.environment["MONTY_TEST_SERVER"].flatMap(URL.init(string:))

/// The fixture sites' addresses, which the dev server writes next to the repository's data.
func site(_ name: String) throws -> String {
    let file = URL(filePath: #filePath).deletingLastPathComponent().appending(path: "../../../data/mac-dev-sites.json").standardized
    let sites = try JSONSerialization.jsonObject(with: Data(contentsOf: file)) as? [String: String]
    return try #require(sites?[name], "no \(name) in \(file.path)")
}

/// The app as launched on the Mac of the person `id`: their cookies and settings, kept between launches.
@MainActor
func launch(_ id: String) -> AppModel {
    AppModel(
        serverURL: server!,
        cookies: HTTPCookieStorage.sharedCookieStorage(forGroupContainerIdentifier: "monty-test-\(id)"),
        defaults: UserDefaults(suiteName: "monty-test-\(id)")!
    )
}

/// A fresh person: their own cookies and settings, a new account.
@MainActor
func person(_ id: String = UUID().uuidString) async throws -> AppModel {
    let app = launch(id)
    await app.start()
    #expect(app.phase == .signedOut)
    try await app.signUp(email: "mac-\(id.prefix(8).lowercased())@example.test", password: "correct horse")
    return app
}

/// Waits for what a person would wait for, up to `seconds`.
@MainActor
func eventually(_ what: String, seconds: Double = 30, _ condition: () -> Bool) async throws {
    let deadline = Date().addingTimeInterval(seconds)
    while !condition() {
        if Date() > deadline { Issue.record("timed out waiting for \(what)"); throw CancellationError() }
        try await Task.sleep(for: .milliseconds(100))
    }
}

@MainActor
func say(_ text: String, in app: AppModel) async throws -> ChatModel {
    let chat = try #require(app.chat)
    chat.draft = text
    #expect(chat.canSend)
    await chat.send()
    #expect(chat.draft.isEmpty)
    return chat
}

@Suite(.enabled(if: server != nil, "set MONTY_TEST_SERVER to a dev server"), .serialized)
@MainActor
struct JourneyTests {
    @Test func aNewChatGetsAReplyAndIsListed() async throws {
        let app = try await person()
        #expect(app.route == .chat(nil))
        let chat = try await say("Say hello", in: app)

        // What was sent shows at once, and the chat takes no second message while Monty works.
        #expect(chat.shownMessages.first == ChatMessage(role: .user, text: "Say hello"))
        let id = try #require(chat.threadId)
        #expect(app.route == .chat(id))
        #expect(app.threads.first?.id == id)

        try await eventually("the reply") { chat.messages.last?.role == .assistant }
        #expect(chat.messages.last?.text.contains("Hello") == true)
        #expect(chat.run?.status == .done && !chat.isActive && chat.preview == nil && chat.pendingMessage == nil)
        await app.loadThreads()
        #expect(app.threads.map(\.id) == [id])
        #expect(app.threads.first?.status == nil)
    }

    @Test func aQuestionIsAnsweredFromTheChat() async throws {
        let app = try await person()
        let chat = try await say("Ask me my favourite colour and remember it.", in: app)
        try await eventually("the question") { chat.ask?.kind == .question }
        await app.loadThreads()
        #expect(app.needsYou.map(\.id) == [chat.threadId])
        #expect(!chat.canSend)  // the answer goes in the question, not the composer

        await chat.answer(.text("green"))
        try await eventually("the reply") { chat.run?.status == .done }
        #expect(chat.messages.last?.text.lowercased().contains("green") == true)
        await app.loadMemories()
        #expect(app.memories?.contains { $0.text.lowercased().contains("green") } == true)
        await app.loadThreads()
        #expect(app.needsYou.isEmpty)
    }

    @Test func signInThroughTheLiveViewThenApproveTheOrder() async throws {
        let app = try await person()
        let chat = try await say("Order eggs from \(try site("shop"))", in: app)
        try await eventually("the hand-off") { chat.ask?.kind == .handoff }

        await chat.takeOver()
        let live = try #require(chat.live)
        try await eventually("the first frame") { live.state == .driving && live.frame != nil }
        try await eventually("the shop's tab") { live.activeTab?.url.hasPrefix("http") == true }
        let start = live.activeTab!.url
        #expect(live.reason.lowercased().contains("sign in"))

        // As a person would: type into the focused field, Tab, the password, Enter.
        live.type("alice")
        live.input(.press(key: "Tab", modifiers: []))
        live.type("hunter2")
        live.input(.press(key: "Enter", modifiers: []))
        try await eventually("the signed-in page") { live.activeTab?.url != start }
        live.giveBack()
        try await eventually("the give-back") { live.state == .ended(givenBack: true) }
        await chat.liveViewEnded()
        #expect(chat.live == nil)

        try await eventually("the approval") { chat.ask?.kind == .approval }
        #expect(chat.ask?.prompt.lowercased().contains("eggs") == true)
        await chat.answer(.approve())
        try await eventually("the order") { chat.run?.status == .done }
        // The dev server's shop numbers orders across all test runs.
        #expect(chat.messages.last?.text.range(of: #"#\d+"#, options: .regularExpression) != nil)

        // Opened again later, the chat still reads, with what happened along the way.
        let id = try #require(chat.threadId)
        app.open(.chat(nil))
        app.open(.chat(id))
        let again = try #require(app.chat)
        try await eventually("the chat again") { !again.messages.isEmpty }
        #expect(again.loadError == nil)
        #expect(again.messages.contains { $0.role == .event && $0.text.hasPrefix("You approved") })
        #expect(again.messages.contains { $0.role == .event && $0.text.hasPrefix("You took over") })

        await app.loadSavedSites()
        #expect(app.savedSites?.contains { $0.site == "127.0.0.1" } == true)
    }

    @Test func stoppingARunThatWaitsFreesTheChat() async throws {
        let app = try await person()
        let chat = try await say("Ask me my favourite colour and remember it.", in: app)
        try await eventually("the question") { chat.ask != nil }
        await chat.stop()
        #expect(chat.run?.status == .stopped)
        #expect(chat.ask == nil && !chat.isActive)
        chat.draft = "Say hello"
        #expect(chat.canSend)
    }

    @Test func otherChatsNotifyWhenTheyNeedYouOrFinish() async throws {
        let app = try await person()
        var notices: [Notice] = []
        app.notify = { notices.append($0) }

        let asking = try await say("Ask me my favourite colour and remember it.", in: app)
        let askingId = try #require(asking.threadId)
        try await eventually("the question") { asking.ask != nil }
        app.open(.chat(nil))  // the user looks at something else
        await app.loadThreads()
        #expect(notices.isEmpty)  // it was on screen when it asked

        let hello = try await say("Say hello", in: app)
        let helloId = try #require(hello.threadId)
        app.open(.chat(askingId))
        try await eventually("the finished notice", seconds: 20) {
            Task { await app.loadThreads() }
            return notices.contains { $0.kind == .finished && $0.threadId == helloId }
        }
        #expect(notices.first { $0.threadId == helloId }?.body.contains("Hello") == true)

        // A question while the app is in the background is a notice even for the open chat.
        try await eventually("the chat to load") { app.chat?.ask != nil }
        await app.chat?.answer(.text("blue"))
        app.isActive = false
        app.open(.chat(helloId))
        let again = try await say("Ask me my favourite colour and remember it.", in: app)
        try await eventually("the question notice", seconds: 20) {
            Task { await app.loadThreads() }
            return notices.contains { $0.kind == .question && $0.threadId == again.threadId }
        }
        #expect(notices.filter { $0.kind == .question }.count == 1)
        #expect(notices.first { $0.kind == .question }?.title == "Monty has a question")
    }

    @Test func draftsStayWithTheirChat() async throws {
        let app = try await person()
        let first = try await say("Say hello", in: app)
        let id = try #require(first.threadId)
        try await eventually("the reply") { first.run?.status == .done }
        first.draft = "half a thought"
        app.open(.chat(nil))
        app.chat?.draft = "a new task"
        app.open(.chat(id))
        try await eventually("the chat") { app.chat?.threadId == id }
        #expect(app.chat?.draft == "half a thought")
        app.open(.chat(nil))
        #expect(app.chat?.draft == "a new task")
    }

    @Test func signingOutAndBackIn() async throws {
        let app = try await person()
        let chat = try await say("Say hello", in: app)
        try await eventually("the reply") { chat.run?.status == .done }
        let email = try #require(app.user?.email)

        await app.signOut()
        #expect(app.phase == .signedOut && app.threads.isEmpty && app.chat == nil)
        #expect(!app.client.hasSessionCookie)
        do {
            try await app.signIn(email: email, password: "wrong password")
            Issue.record("signed in with the wrong password")
        } catch let error as APIError {
            #expect(error.localizedDescription == "Wrong email or password.")
        }
        try await app.signIn(email: " \(email) ", password: "correct horse")
        await app.loadThreads()
        #expect(app.threads.count == 1)
    }

    @Test func aSessionThatEndsSendsYouToSignIn() async throws {
        let app = try await person()
        app.client.clearSession()  // as if it expired
        await app.loadThreads()
        #expect(app.phase == .signedOut)
        #expect(app.signedOutReason == "Your session ended. Sign in again to carry on.")
    }

    @Test func schedulesCanBePausedResumedAndDeleted() async throws {
        let app = try await person()
        let chat = try await say("Tell me when a delivery slot opens at \(try site("slots"))", in: app)
        try await eventually("the schedule's approval") { chat.ask?.kind == .approval }
        await chat.answer(.approve())
        try await eventually("the reply") { chat.run?.status == .done }
        await app.loadSchedules()
        let schedule = try #require(app.schedules?.first)
        #expect(schedule.name == "Delivery slot" && schedule.plainWhen == "every 30 minutes" && schedule.watch)
        await app.loadThreads()
        #expect(app.threads.contains { $0.id == schedule.threadId })  // a schedule reports in a chat of its own

        let next = try #require(schedule.nextRun, "when it runs next")
        #expect(next > .now && next < .now.addingTimeInterval(31 * 60))  // every 30 minutes

        await app.setPaused(schedule, true)
        #expect(app.schedules?.first?.paused == true && app.schedules?.first?.nextRun == nil)  // paused: never next
        await app.setPaused(schedule, false)
        #expect(app.schedules?.first?.paused == false && app.schedules?.first?.nextRun != nil)
        await app.delete(schedule)
        #expect(app.schedules == [])
        await app.loadSchedules()
        #expect(app.schedules == [])
    }

    @Test func downloadedFilesCanBeSaved() async throws {
        let app = try await person()
        let chat = try await say("Download my last three invoices from \(try site("invoices"))", in: app)
        try await eventually("the reply", seconds: 60) { chat.run?.status == .done || chat.run?.status == .failed }
        #expect(chat.run?.status == .done)
        await app.loadFiles()
        let files = try #require(app.files?.files)
        #expect(files.count >= 3)
        let file = try #require(files.first)
        let downloaded = try #require(await app.download(file))
        #expect(downloaded.data.count == file.size)
        #expect(downloaded.name == file.name)
    }

    @Test func aFailedRunSaysSoAndFreesTheChat() async throws {
        let app = try await person()
        let chat = try await say("Fail please", in: app)
        try await eventually("the failure") { chat.run?.status == .failed }
        #expect(chat.messages.last?.role == .assistant)
        #expect(!chat.isActive)
    }

    // Regressions from the first review round.

    @Test func anAnswerTypedForAQuestionAnsweredElsewhereIsKept() async throws {
        let app = try await person()
        let chat = try await say("Ask me my favourite colour and remember it.", in: app)
        try await eventually("the question") { chat.ask != nil }
        chat.answerDraft = "purple, I think"
        try await app.client.answer(try #require(chat.ask?.id), .text("blue"))  // the web app, meanwhile
        try await eventually("the question to close") { chat.ask == nil }
        #expect(chat.draft == "purple, I think")
        #expect(chat.notice?.isError == false)
    }

    @Test func sendingTwiceQuicklyStartsOneTask() async throws {
        let app = try await person()
        let chat = try #require(app.chat)
        chat.draft = "Ask me my favourite colour and remember it."
        async let first: Void = chat.send()
        async let second: Void = chat.send()
        _ = await (first, second)
        try await eventually("the question") { chat.ask != nil }
        await app.loadThreads()
        #expect(app.threads.count == 1)
        #expect(chat.run?.status == .waiting)  // not stopped by a second click
    }

    @Test func leavingANewChatMidSendKeepsTheNextDraft() async throws {
        let app = try await person()
        let chat = try #require(app.chat)
        chat.draft = "Say hello"
        let sending = Task { await chat.send() }
        await Task.yield()  // the send is on its way to the server
        app.open(.schedules)
        app.open(.chat(nil))
        app.chat?.draft = "my next task"
        await sending.value
        app.open(.files)
        app.open(.chat(nil))
        #expect(app.chat?.draft == "my next task")
        await app.loadThreads()
        #expect(app.threads.count == 1)  // the first message still went
    }

    @Test func takingOverTwiceOpensOneLiveView() async throws {
        let app = try await person()
        let chat = try await say("Order eggs from \(try site("shop"))", in: app)
        try await eventually("the hand-off") { chat.ask?.kind == .handoff }
        async let first: Void = chat.takeOver()
        async let second: Void = chat.takeOver()
        _ = await (first, second)
        let live = try #require(chat.live)
        try await eventually("the first frame") { live.frame != nil }
        try await Task.sleep(for: .seconds(1))
        #expect(live.state == .driving)  // not pushed out by a second connection
        live.close()
        await chat.stop()
    }

    // Regressions from the second review round.

    @Test func anAnswerRefusedAsAlreadyAnsweredIsKept() async throws {
        let app = try await person()
        let chat = try await say("Ask me my favourite colour and remember it.", in: app)
        try await eventually("the question") { chat.ask != nil }
        let ask = try #require(chat.ask)
        try await app.client.answer(ask.id, .text("blue"))  // the web app wins the race...
        chat.answerDraft = "a long, careful answer"
        await chat.answer(.text("a long, careful answer"))  // ...before this one is seen to be stale
        try await eventually("the text to come back") { chat.draft.contains("a long, careful answer") }
        #expect(chat.notice?.isError == false)
    }

    @Test func aFailedTaskElsewhereNotifiesThatItFailed() async throws {
        let app = try await person()
        var notices: [Notice] = []
        app.notify = { notices.append($0) }
        let hello = try await say("Say hello", in: app)
        try await eventually("the reply") { hello.run?.status == .done }
        app.open(.chat(nil))
        let failing = try await say("Fail please", in: app)
        let id = try #require(failing.threadId)
        app.open(.chat(hello.threadId))  // looking elsewhere when it fails
        try await eventually("the failure notice", seconds: 20) {
            Task { await app.loadThreads() }
            return notices.contains { $0.threadId == id }
        }
        #expect(notices.first { $0.threadId == id }?.body == "Monty couldn't finish this task.")
    }

    @Test func aChatFirstSeenWaitingNotifies() async throws {
        let app = try await person()
        var notices: [Notice] = []
        app.notify = { notices.append($0) }
        await app.loadThreads()
        let created = try await app.client.startThread("Ask me my favourite colour and remember it.")  // another device
        try await eventually("the question notice", seconds: 20) {
            Task { await app.loadThreads() }
            return notices.contains { $0.threadId == created.threadId && $0.kind == .question }
        }
    }

    @Test func takingOverCannotBeAbandonedByNavigating() async throws {
        let app = try await person()
        let chat = try await say("Order eggs from \(try site("shop"))", in: app)
        try await eventually("the hand-off") { chat.ask?.kind == .handoff }
        await chat.takeOver()
        try await eventually("the live view") { chat.live?.frame != nil }
        app.open(.chat(nil))  // ⌘N, mid sign-in
        #expect(app.chat === chat && app.isTakingOver)
        chat.leaveLiveView()
        app.open(.chat(nil))
        #expect(app.chat !== chat)
    }

    // Regressions from the final review round.

    @Test func aHalfWrittenAnswerComesBackAfterLookingElsewhere() async throws {
        let app = try await person()
        let chat = try await say("Ask me my favourite colour and remember it.", in: app)
        let id = try #require(chat.threadId)
        try await eventually("the question") { chat.ask != nil }
        let ask = try #require(chat.ask)
        chat.answerDraft = "teal, or maybe"
        app.open(.schedules)  // looks elsewhere...
        try await app.client.answer(ask.id, .text("blue"))  // ...while it is answered in the web app
        app.open(.chat(id))
        try await eventually("the half-written answer") { app.chat?.draft == "teal, or maybe" }
        #expect(app.chat?.notice?.text.contains("in the message box") == true)
    }

    @Test func typingWhileANewChatIsCreatedStaysWithIt() async throws {
        let app = try await person()
        let chat = try #require(app.chat)
        chat.draft = "Say hello"
        let sending = Task { await chat.send() }
        await Task.yield()
        chat.draft = "and then"  // typed while it sends
        await sending.value
        let id = try #require(chat.threadId)
        app.open(.chat(nil))
        #expect(app.chat?.draft == "")  // the next new task starts empty
        app.open(.chat(id))
        #expect(app.chat?.draft == "and then")
    }

    // Managing chats, and taking over with VoiceOver.

    @Test func chatsSayHowTheyEndedAndCanBeRenamedAndDeleted() async throws {
        let app = try await person()
        let failing = try await say("Fail please", in: app)
        try await eventually("the failure") { failing.run?.status == .failed }
        app.open(.chat(nil))
        let asking = try await say("Ask me my favourite colour and remember it.", in: app)
        try await eventually("the question") { asking.ask != nil }
        await app.loadThreads()
        let failed = try #require(app.threads.first { $0.id == failing.threadId })
        #expect(failed.status == nil && failed.outcome == .failed)

        await app.rename(failed, to: "  Broken thing  ")
        #expect(app.threads.first { $0.id == failed.id }?.title == "Broken thing")
        await app.loadThreads()
        #expect(app.threads.first { $0.id == failed.id }?.title == "Broken thing")

        // Deleting the chat on screen, mid-question: the task stops and a new task takes its place.
        let waiting = try #require(app.threads.first { $0.id == asking.threadId })
        await app.delete(waiting)
        #expect(app.route == .chat(nil) && app.chat?.threadId == nil)
        #expect(!app.threads.contains { $0.id == waiting.id } && app.needsYou.isEmpty)
        await app.loadThreads()
        #expect(app.threads.map(\.id) == [failed.id])
    }

    // The basics: trying again, and coming back to where you were.

    @Test func aStoppedTaskIsTriedAgainAsItWasAsked() async throws {
        let app = try await person()
        let prompt = "Ask me my favourite colour and remember it."
        let chat = try await say(prompt, in: app)
        try await eventually("the question") { chat.ask != nil }
        #expect(chat.canStop && !chat.canRetry)
        await chat.stop()
        #expect(chat.run?.status == .stopped)
        #expect(chat.canRetry && chat.lastTask == prompt)

        chat.draft = "something else"
        chat.editLastTask()
        #expect(chat.draft == prompt + "\n\nsomething else")
        chat.draft = ""

        await chat.retry()
        #expect(!chat.canRetry)
        try await eventually("the same question again") { chat.ask != nil }
        #expect(chat.run?.prompt == prompt)
        #expect(chat.messages.filter { $0.role == .user && $0.text == prompt }.count == 2)
        await chat.stop()
    }

    @Test func aFailedTaskIsTriedAgain() async throws {
        let app = try await person()
        let chat = try await say("Fail please", in: app)
        try await eventually("the failure") { chat.run?.status == .failed }
        let failed = try #require(chat.run?.id)
        #expect(chat.canRetry)
        await chat.retry()
        try await eventually("the new run to fail too") { chat.run?.id != failed && chat.run?.status == .failed }
        #expect(chat.messages.filter { $0.role == .user }.map(\.text) == ["Fail please", "Fail please"])
    }

    @Test func relaunchingComesBackToTheSameChatAndDrafts() async throws {
        let id = UUID().uuidString
        let app = try await person(id)
        let chat = try await say("Say hello", in: app)
        let thread = try #require(chat.threadId)
        try await eventually("the reply") { chat.run?.status == .done }
        chat.draft = "half a thought"
        app.open(.chat(nil))
        app.chat?.draft = "a new task"
        app.open(.chat(thread))
        #expect(app.openThread?.id == thread)

        // Quit, and open the app again: signed in, in the same chat, with what was being written.
        let again = launch(id)
        await again.start()
        #expect(again.user?.email == app.user?.email)
        #expect(again.route == .chat(thread))
        try await eventually("the chat") { again.chat?.messages.isEmpty == false }
        #expect(again.chat?.draft == "half a thought")
        again.open(.chat(nil))
        #expect(again.chat?.draft == "a new task")

        // Signing out on purpose forgets them; the next sign-in starts afresh.
        let email = try #require(again.user?.email)
        await again.signOut()
        try await again.signIn(email: email, password: "correct horse")
        #expect(again.route == .chat(nil) && again.chat?.draft == "")
        #expect(again.drafts.isEmpty)
    }

    @Test func relaunchingIntoADeletedChatStartsANewTask() async throws {
        let id = UUID().uuidString
        let app = try await person(id)
        let chat = try await say("Say hello", in: app)
        let thread = try #require(app.openThread)
        try await eventually("the reply") { chat.run?.status == .done }
        try await app.client.delete(thread: thread.id)  // from the web app, while this Mac's app is closed

        let again = launch(id)
        await again.start()
        try await eventually("a new task instead") { again.route == .chat(nil) && again.chat?.threadId == nil }
    }

    @Test func aTaskThatFinishesUnseenIsMarkedUntilOpened() async throws {
        let app = try await person()
        var seen: [String] = []
        app.chatSeen = { seen.append($0) }
        let hello = try await say("Say hello", in: app)
        let id = try #require(hello.threadId)
        await app.loadThreads()  // seen working
        app.open(.chat(nil))  // and the user looks elsewhere while it finishes
        try await eventually("the unseen mark") {
            Task { await app.loadThreads() }
            return app.unseen.contains(id)
        }
        app.open(.chat(id))
        #expect(!app.unseen.contains(id) && seen.contains(id))  // its notification goes too
    }

    @Test func aTaskThatFinishedWhileTheAppWasClosedIsMarked() async throws {
        let id = UUID().uuidString
        let app = try await person(id)
        let chat = try await say("Ask me my favourite colour and remember it.", in: app)
        let thread = try #require(chat.threadId)
        try await eventually("the question") { chat.ask != nil }
        let ask = try #require(chat.ask?.id)
        await app.loadThreads()  // the app last saw it waiting
        app.open(.chat(nil))
        try await app.client.answer(ask, .text("green"))  // answered on the web while the Mac app is quit
        try await eventually("the task to finish") {
            Task { await app.loadThreads() }
            return app.threads.first { $0.id == thread }?.status == nil
        }

        let again = launch(id)
        again.isActive = false  // launched in the background, as at login
        await again.start()
        await again.loadThreads()
        #expect(again.unseen.contains(thread))
    }

    @Test func aRenameThatFailsSaysSoAndADeleteOfAGoneChatJustWorks() async throws {
        let app = try await person()
        let chat = try await say("Say hello", in: app)
        try await eventually("the reply") { chat.run?.status == .done }
        let thread = try #require(app.openThread)
        try await app.client.delete(thread: thread.id)  // deleted on the web meanwhile

        await app.rename(thread, to: "New name")
        #expect(app.actionError?.hasPrefix("Couldn't rename the chat.") == true)
        app.actionError = nil

        await app.delete(thread)
        #expect(app.actionError == nil)
        #expect(!app.threads.contains { $0.id == thread.id } && app.route == .chat(nil))
    }

    @Test func pinnedChatsStayOnTopAcrossLaunches() async throws {
        let id = UUID().uuidString
        let app = try await person(id)
        let first = try await say("Say hello", in: app)
        try await eventually("the first reply") { first.run?.status == .done }
        let older = try #require(app.openThread)
        app.open(.chat(nil))
        let second = try await say("Say hello", in: app)
        try await eventually("the second reply") { second.run?.status == .done }
        await app.loadThreads()
        #expect(app.sidebarOrder.map(\.id).first != older.id)  // the newest is first...
        #expect(app.threads.allSatisfy { $0.updatedAt != nil })  // ...as the server says, with when

        app.setPinned(older, true)
        #expect(app.sidebarOrder.first?.id == older.id)  // ...unless one is pinned
        let again = launch(id)
        await again.start()
        await again.loadThreads()
        #expect(again.pinned == [older.id] && again.sidebarOrder.first?.id == older.id)
        again.setPinned(older, false)
        #expect(again.pinned.isEmpty)
    }

    @Test func aNewMessageInAnOldChatMovesItToTheTop() async throws {
        let app = try await person()
        let first = try await say("Say hello", in: app)
        try await eventually("the first reply") { first.run?.status == .done }
        let older = try #require(first.threadId)
        app.open(.chat(nil))
        let second = try await say("Say hello", in: app)
        try await eventually("the second reply") { second.run?.status == .done }
        await app.loadThreads()
        #expect(app.threads.first?.id != older)
        app.open(.chat(older))
        try await eventually("the chat") { app.chat?.messages.isEmpty == false }
        _ = try await say("Say hello again", in: app)
        #expect(app.threads.first?.id == older)  // at once, before the list is read again
        await app.loadThreads()
        #expect(app.threads.first?.id == older)
    }

    @Test func aQuestionIsAnsweredFromItsNotification() async throws {
        let app = try await person()
        var notices: [Notice] = []
        app.notify = { notices.append($0) }
        let asking = try await say("Ask me my favourite colour and remember it.", in: app)
        let thread = try #require(asking.threadId)
        app.open(.chat(nil))  // looking elsewhere when it asks
        try await eventually("the question notice", seconds: 20) {
            Task { await app.loadThreads() }
            return notices.contains { $0.kind == .question && $0.threadId == thread }
        }
        let ask = try #require(notices.first { $0.threadId == thread }?.askId)
        #expect(await app.answerFromNotification(ask, in: thread, text: "  green  "))
        app.open(.chat(thread))
        try await eventually("the reply") { app.chat?.run?.status == .done }
        #expect(app.chat?.messages.contains { $0.role == .user && $0.text == "green" } == true)

        // Answered again from an old notification: not taken, and kept for the user.
        #expect(await !app.answerFromNotification(ask, in: thread, text: "blue"))
        try await eventually("the answer kept") { app.chat?.draft.contains("blue") == true }
    }

    @Test func anApprovalCanBeDeclinedWithAReason() async throws {
        let app = try await person()
        let chat = try await say("Tell me when a delivery slot opens at \(try site("slots"))", in: app)
        try await eventually("the approval") { chat.ask?.kind == .approval }
        chat.denying = true  // the Task menu's Don't Approve…
        await chat.answer(.deny("Not this week"))
        try await eventually("the run to carry on") { chat.ask == nil }
        #expect(!chat.denying && chat.answerError == nil)
        try await eventually("the reply") { chat.run?.status.isActive == false }
        #expect(chat.messages.contains { $0.role == .event && $0.text.hasPrefix("You said no to") })
    }

    @Test func aDeletedChatCanBeUndoneThenIsGoneForGood() async throws {
        let app = try await person()
        let chat = try await say("Say hello", in: app)
        try await eventually("the reply") { chat.run?.status == .done }
        let thread = try #require(app.openThread)

        app.deleteWithUndo(thread)
        #expect(!app.threads.contains { $0.id == thread.id } && app.route == .chat(nil))
        await app.loadThreads()
        #expect(!app.threads.contains { $0.id == thread.id })  // still on the server, but not shown
        app.undoDelete()
        #expect(app.threads.contains { $0.id == thread.id } && app.route == .chat(thread.id))
        #expect(try await app.client.threads().contains { $0.id == thread.id })

        app.undoWindow = .milliseconds(200)
        app.deleteWithUndo(thread)
        try await eventually("the undo to lapse") { app.recentlyDeleted == nil }
        var onServer = true
        for _ in 0..<50 where onServer {  // the server's delete is on its way
            onServer = try await app.client.threads().contains { $0.id == thread.id }
            if onServer { try await Task.sleep(for: .milliseconds(100)) }
        }
        #expect(!onServer)
    }

    @Test func twoDeletesThenSigningOutAtOnceDeleteBoth() async throws {
        let app = try await person()
        let first = try await say("Say hello", in: app)
        try await eventually("the first reply") { first.run?.status == .done }
        let a = try #require(app.openThread)
        app.open(.chat(nil))
        let second = try await say("Say hello", in: app)
        try await eventually("the second reply") { second.run?.status == .done }
        let b = try #require(app.openThread)
        let email = try #require(app.user?.email)

        app.deleteWithUndo(a)
        app.deleteWithUndo(b)  // the first is deleted now; the second can still be undone...
        await app.signOut()  // ...until signing out, which deletes it and waits for both
        try await app.signIn(email: email, password: "correct horse")
        await app.loadThreads()
        #expect(!app.threads.contains { $0.id == a.id || $0.id == b.id })
    }

    @Test func aMessageQueuedWhileMontyWorksGoesWhenItIsDone() async throws {
        let app = try await person()
        let chat = try await say("Say hello", in: app)
        try #require(chat.isActive, "the scripted run finished before the follow-up could be queued")
        chat.draft = "Say hello again"
        #expect(chat.canQueue && !chat.canSend)
        chat.queue()
        #expect(chat.queued == "Say hello again" && chat.draft.isEmpty)
        try await eventually("the queued message to go") {
            chat.queued == nil && chat.messages.contains { $0.role == .user && $0.text == "Say hello again" }
        }
        try await eventually("its reply") { chat.run?.status == .done }
        #expect(chat.run?.started != nil && chat.run?.completed != nil)  // "Worked for …"
    }

    @Test func backAndForwardGoWhereTheUserWas() async throws {
        let app = try await person()
        let first = try await say("Say hello", in: app)
        let a = try #require(first.threadId)
        try await eventually("the first reply") { first.run?.status == .done }
        app.open(.schedules)
        app.open(.chat(nil))
        let second = try await say("Say hello", in: app)
        let b = try #require(second.threadId)
        try await eventually("the second reply") { second.run?.status == .done }

        app.goBack()
        #expect(app.route == .schedules)  // the new task became chat b: back goes before it
        app.goBack()
        #expect(app.route == .chat(a))
        app.goForward()
        #expect(app.route == .schedules)
        await app.delete(try #require(app.threads.first { $0.id == b }))
        app.goForward()
        #expect(app.route != .chat(b))  // gone: skipped
    }

    @Test func anEarlierReplyKeepsWhatMontyDid() async throws {
        let app = try await person()
        let chat = try await say("Find the three cheapest flights to Lisbon next Friday at \(try site("flights"))", in: app)
        try await eventually("the first reply") { chat.run?.status == .done }
        let browsed = chat.run?.activity ?? []
        try #require(!browsed.isEmpty, "the scripted flights task opened no pages")
        let reply = try #require(chat.messages.lastIndex { $0.role == .assistant })
        chat.draft = "Say hello"
        await chat.send()
        try await eventually("the second reply") { chat.run?.status == .done && chat.messages.count > reply + 1 }

        let id = try #require(chat.threadId)
        app.open(.chat(nil))
        app.open(.chat(id))  // read again from the server
        let reopened = try #require(app.chat)
        try await eventually("the chat to load") { reopened.messages.count == chat.messages.count }
        let past = try #require(reopened.pastSteps[reply], "the first reply's steps")
        #expect(past.activity == browsed)
        #expect(past.summary.hasPrefix("Worked for "))
    }

    @Test func chatsAreFoundByWhatMontySaid() async throws {
        let app = try await person()
        let chat = try await say("Say hello", in: app)
        try await eventually("the reply") { chat.run?.status == .done }
        let reply = try #require(chat.messages.last { $0.role == .assistant }?.text)
        // A word only the reply has: found by the task or the title, it would prove nothing about replies.
        let asked = "say hello"
        let word = try #require(
            reply.split(whereSeparator: { !$0.isLetter }).map(String.init).first { $0.count >= 4 && !asked.contains($0.lowercased()) },
            "a word only in: \(reply)"
        )
        #expect(await app.search(word).contains(try #require(chat.threadId)))
        #expect(await app.search("zz-nothing-says-this-zz").isEmpty)
    }

    @Test func theChatListSaysWhatAChatWaitsFor() async throws {
        let app = try await person()
        let chat = try await say("Tell me when a delivery slot opens at \(try site("slots"))", in: app)
        let id = try #require(chat.threadId)
        try await eventually("the approval") { chat.ask?.kind == .approval }
        #expect(app.threads.first { $0.id == id }?.waitingFor == .approval)  // at once, from the open chat
        await app.loadThreads()
        #expect(app.threads.first { $0.id == id }?.waitingFor == .approval)  // and as the server says
        await chat.answer(.approve())
        try await eventually("the reply") { chat.run?.status == .done }
        await app.loadThreads()
        let done = try #require(app.threads.first { $0.id == id })
        #expect(done.waitingFor == nil)

        app.markUnread(done)
        #expect(app.unseen.contains(id))
        app.markSeen(done)
        #expect(!app.unseen.contains(id))
    }

    @Test func signingInWithVoiceOverThroughTheOutline() async throws {
        let app = try await person()
        let chat = try await say("Order eggs from \(try site("shop"))", in: app)
        try await eventually("the hand-off") { chat.ask?.kind == .handoff }
        await chat.takeOver()
        let live = try #require(chat.live)
        live.wantsOutline = true  // as when VoiceOver asks the canvas for its children
        try await eventually("the sign-in form's outline") { live.outline?.items.contains { $0.role == "textbox" } == true }
        let outline = try #require(live.outline)
        #expect(outline.available)
        #expect(outline.items.contains { $0.role == "button" })

        func press(_ role: String, where matches: (OutlineItem) -> Bool) throws {
            let item = try #require(live.outline?.items.first { $0.role == role && matches($0) })
            live.input(.mouseDown(x: item.centre.x, y: item.centre.y, button: .left))
            live.input(.mouseUp(x: item.centre.x, y: item.centre.y, button: .left))
        }
        let fields = outline.items.filter { $0.role == "textbox" }
        try press("textbox") { $0 == fields[0] }
        live.type("alice")
        try press("textbox") { $0 == fields[1] }
        live.type("hunter2")
        try await eventually("the typed password, as a count") { live.outline?.items.contains { $0.secure && $0.value == "7 characters" } == true }
        #expect(live.outline.map { "\($0)" }?.contains("hunter2") == false)
        try press("button") { _ in true }
        try await eventually("the signed-in page") { live.activeTab.map { !$0.url.contains("login") } == true }
        live.giveBack()
        try await eventually("the give-back") { live.state.isOver }
        await chat.liveViewEnded()
        try await eventually("the approval") { chat.ask?.kind == .approval }
        await chat.stop()
    }

    @Test func aForgottenPasswordCanBeResetWithoutEmailSetUp() async throws {
        // The dev server has no SMTP: asking for a code still answers the same, and a wrong code is refused plainly.
        let app = try await person()
        let email = try #require(app.user?.email)
        await app.signOut()
        try await app.requestPasswordReset(email: email)
        do {
            try await app.resetPassword(email: email, code: "000000", password: "a new password")
            Issue.record("a made-up code worked")
        } catch let error as APIError {
            #expect(error.localizedDescription == "That code is wrong or has expired; ask for a new one.")
        }
        #expect(app.phase == .signedOut)
    }
}
