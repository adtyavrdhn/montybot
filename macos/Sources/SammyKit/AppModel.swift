import Foundation
import Observation
@preconcurrency import OpenTelemetryApi

public enum Route: Hashable, Sendable {
    /// A chat; nil is a new one.
    case chat(String?)
    case schedules
    case signIns
    case integrations
    case memory

    /// The route as kept in settings, to come back to it at the next launch.
    var stored: String {
        switch self {
        case .chat(let id): "chat:" + (id ?? "")
        case .schedules: "schedules"
        case .signIns: "signIns"
        case .integrations: "integrations"
        case .memory: "memory"
        }
    }

    /// nil for a page there is no longer ("files", from before files moved into the chats): a new task opens instead.
    init?(stored: String) {
        switch stored {
        case "schedules": self = .schedules
        case "signIns": self = .signIns
        case "integrations": self = .integrations
        case "memory": self = .memory
        default:
            guard stored.hasPrefix("chat:") else { return nil }
            let id = String(stored.dropFirst(5))
            self = .chat(id.isEmpty ? nil : id)
        }
    }
}

/// An answer the user is writing, and the chat its question is in.
public struct AnswerDraft: Equatable, Sendable {
    public let threadId: String
    public var text: String
}

/// Something the user should hear about while they are not looking at it.
public struct Notice: Equatable, Sendable {
    public enum Kind: String, Sendable { case question, approval, handoff, connect, finished }
    public let kind: Kind
    public let threadId: String
    public let title: String
    public let body: String
    /// The question or approval it is about, so a question can be answered from the notification.
    public var askId: String? = nil
}

/// The whole app's state: who is signed in, their chats, the open page, and their schedules, memories and so on.
@MainActor
@Observable
public final class AppModel {
    public enum Phase: Equatable {
        case launching
        /// The server can't be reached at launch; the saved session is kept and the app keeps trying.
        case unreachable
        /// The server is private: its site login is needed before anything else (`realm` names it).
        case siteLogin(realm: String)
        case signedOut
        case signedIn(User)
    }

    public private(set) var phase: Phase = .launching
    public private(set) var client: APIClient
    public private(set) var threads: [ThreadSummary] = []
    public private(set) var threadsLoaded = false
    public private(set) var route: Route = .chat(nil) {
        didSet { if let id = persistedUser { defaults.set(route.stored, forKey: "route.\(id)") } }
    }
    public private(set) var chat: ChatModel?
    /// Why the user is looking at the sign-in screen, when it is not their first time: "Your session ended".
    public private(set) var signedOutReason: String?
    /// The server could not be reached the last time the chat list was read.
    public private(set) var offline = false

    public private(set) var schedules: [Schedule]?
    public private(set) var savedSites: [SavedSite]?
    public private(set) var memories: [Memory]?
    public private(set) var integrations: Integrations?
    /// What the Integrations page lists (featured by kind, then every other app), once read: it changes rarely.
    public private(set) var apps: [CatalogApp]?
    /// A name for the add-server form, from a chat's "Add an MCP server".
    public var serverName = ""
    /// What adding an MCP server came to, or why it didn't work, for the form to say.
    public var serverNote: ChatNotice?
    /// Opens a sign-in page in the user's browser (set by the app; the server hears when they are done).
    public var openInBrowser: (@MainActor (URL) -> Void)?
    public var libraryError: String?

    /// Sammy's browser goes out to the web from this Mac, for tasks the user starts, while the app is open and signed
    /// in (`MacTunnel`): sites see the user's own address. On unless the user turns it off in Settings.
    public var browseFromMac: Bool {
        didSet {
            defaults.set(browseFromMac, forKey: "browseFromMac")
            updateTunnel()
        }
    }
    /// The tunnel's state, for the "browsing from your Mac" indicator.
    public private(set) var tunnelStatus = MacTunnel.Status()
    @ObservationIgnored private var tunnel: MacTunnel?
    /// Counts tunnels opened, so a late status from a stopped one is ignored.
    @ObservationIgnored private var tunnels = 0

    /// What the user is typing in each chat ("new" for a new one), kept while they look elsewhere and after they quit.
    public var drafts: [String: String] = [:] {
        didSet { if let id = persistedUser { defaults.set(drafts.filter { !$0.value.isEmpty }, forKey: "drafts.\(id)") } }
    }
    /// The files the user is adding to their next message in each chat ("new" for a new one), as `drafts`. Not kept
    /// after they quit: the server forgets uploads that were never sent within a day.
    public var draftAttachments: [String: [PendingAttachment]] = [:]
    /// Files in chats already fetched (or just uploaded), by id, so a picture is fetched once.
    @ObservationIgnored private var attachmentCache: [String: Data] = [:]
    @ObservationIgnored private var attachmentCacheBytes = 0
    /// A chat the user asked to delete or rename, from wherever they asked (sidebar, toolbar, menu): the window asks
    /// them to confirm, or for the new title.
    public var deleting: ThreadSummary?
    public var renaming: ThreadSummary?
    /// The command palette is open (⌘K), and the sidebar's search should take the keyboard (⌘F). Kept here, not sent
    /// as notifications: a request made while the window is closed waits for it to open.
    public var showingPalette = false
    public var wantsFindChats = false
    /// Something the user did to a chat (rename, delete) didn't work: the window says so.
    public var actionError: String?
    /// A chat the user deleted a moment ago. It is gone from the list at once, but stays on the server until the
    /// moment passes, so they can undo it, as in T3 Code and Mail; then (or at sign-out, or quitting) it is deleted.
    public private(set) var recentlyDeleted: ThreadSummary?
    /// How long a deletion can be undone.
    var undoWindow: Duration = .seconds(8)
    private var pendingDelete: Task<Void, Never>?
    /// Deletions sent to the server and not answered yet: waited for before quitting or signing out, and kept off
    /// the list meanwhile.
    private var deletesInFlight: [String: Task<Void, Never>] = [:]
    /// A chat is being deleted, or can still be undone: quitting waits for it.
    public var hasPendingDeletes: Bool { recentlyDeleted != nil || !deletesInFlight.isEmpty }
    /// Where the user has been, for Back and Forward (⌘[ and ⌘]), as in a browser.
    private var backStack: [Route] = []
    private var forwardStack: [Route] = []
    private var travelling = false
    /// Where the deleted chat was in the list, and whether it was open, to put it back as it was.
    private var deletedFrom: (index: Int, wasOpen: Bool) = (0, false)
    /// Chats the user pinned to the top of the sidebar, in the order they pinned them. Kept on this Mac, through
    /// signing out: a preference, not something they wrote.
    public private(set) var pinned: [String] = [] {
        didSet { if let id = persistedUser { defaults.set(pinned, forKey: "pinned.\(id)") } }
    }
    /// What the user named their squirrel (the mascot beside the composer); empty until they pick one. Kept on this
    /// Mac, per user, like `pinned`.
    public private(set) var squirrelName = "" {
        didSet { if let id = persistedUser { defaults.set(squirrelName, forKey: "squirrelName.\(id)") } }
    }
    public static let squirrelNameLimit = 24
    /// What the app calls the user's own Sammy, the one that works, asks and remembers for them: the name they gave
    /// their squirrel, "Sammy" until they pick one. Plain "Sammy" is the product (the app, its server, signing in).
    public var sammyName: String { squirrelName.isEmpty ? "Sammy" : squirrelName }
    /// Chats that finished while the user was not looking at them, until they open them.
    public private(set) var unseen: Set<String> = [] {
        didSet { if let id = persistedUser { defaults.set(Array(unseen), forKey: "unseen.\(id)") } }
    }
    /// What the user is typing in answer to each open question, by question id, with the chat it is in.
    public var answerDrafts: [String: AnswerDraft] = [:]
    /// Whether the user is driving Sammy's browser: until they hand it back or close it, the app stays there.
    public var isTakingOver: Bool { chat?.live.map { !$0.state.isOver } ?? false }
    /// Whether the app is in front, and its window open; notifications are for when the user is not looking.
    public var isActive = true {
        didSet {
            guard isActive != oldValue else { return }
            telemetry.log(isActive ? "app foregrounded" : "app backgrounded")
            if !isActive { telemetry.flush() }  // the user may quit, or the Mac sleep, from here
            if isActive { seeOpenChat() }
        }
    }
    public var isWindowVisible = true {
        didSet {
            if isWindowVisible { seeOpenChat() } else { chat?.watching = false }  // no pictures for a closed window
        }
    }
    /// Called for each notice; the app shows it in Notification Center.
    public var notify: ((Notice) -> Void)?
    /// Called when the chat list changes (for the Dock badge), and after the user's first task (to ask for
    /// permission to notify, when it makes sense to them).
    public var threadsChanged: (() -> Void)?
    public var firstTaskSent: (() -> Void)?
    /// Called when the user has seen a chat: its notifications are old news.
    public var chatSeen: ((String) -> Void)?
    /// Called when the user signs out or their session ends: every notification was theirs, and is old news.
    public var signedOutOfNotifications: (() -> Void)?

    public var user: User? {
        if case .signedIn(let user) = phase { return user }
        return nil
    }

    public var serverURL: URL { client.baseURL }
    public var telemetry: Telemetry { client.telemetry }
    /// Chats waiting for the user, most recent first.
    public var needsYou: [ThreadSummary] { threads.filter { $0.status == .waiting } }
    public var working: [ThreadSummary] { threads.filter { $0.status?.isWorking == true } }
    /// The chats in the sidebar's order: needing the user, pinned, then the rest.
    public var sidebarOrder: [ThreadSummary] {
        let pins = pinned.compactMap { id in threads.first { $0.id == id && $0.status != .waiting } }
        return needsYou + pins + threads.filter { $0.status != .waiting && !pinned.contains($0.id) }
    }

    private let cookies: HTTPCookieStorage
    private let defaults: UserDefaults
    private var watching: Task<Void, Never>?
    private var known: [String: RunStatus?] = [:]
    private var notified: Set<String> = []
    private var loadingThreads = false
    private var loadAgain = false
    /// Counts changes made here to the chat list; a read that started before one is stale.
    private var localChanges = 0
    private var retrying: Task<Void, Never>?
    /// Whose drafts and place in the app are kept in settings; nil while signed out.
    private var persistedUser: String?
    /// When the app started, for its launch span (sent once telemetry is on, after sign-in).
    private let launched = Date()
    private var launchLogged = false

    public init(serverURL: URL? = nil, cookies: HTTPCookieStorage = .shared, defaults: UserDefaults = .standard) {
        self.cookies = cookies
        self.defaults = defaults
        browseFromMac = defaults.object(forKey: "browseFromMac") as? Bool ?? true
        let url = serverURL ?? defaults.string(forKey: "serverURL").flatMap(URL.init(string:)) ?? Self.defaultServer
        client = APIClient(baseURL: url, cookies: cookies, siteLogin: Self.siteLogins(defaults)[url.absoluteString])
    }

    /// Each private server's site login, as its `Authorization` header, by server URL. Kept in the app's settings
    /// rather than the keychain, which would ask the user for access at each launch of a rebuilt (ad-hoc-signed) app.
    private static func siteLogins(_ defaults: UserDefaults) -> [String: String] {
        defaults.dictionary(forKey: "siteLogins") as? [String: String] ?? [:]
    }

    /// The server this build talks to unless the user picks another: `SammyServerURL` in Info.plist.
    public static var defaultServer: URL {
        (Bundle.main.object(forInfoDictionaryKey: "SammyServerURL") as? String).flatMap(URL.init(string:))
            ?? URL(string: "http://127.0.0.1:8000")!
    }

    // MARK: session

    /// At launch: carry on signed in if the saved session is still good.
    public func start() async {
        do {
            _ = try await client.threads()  // a cheap way to learn whether the server wants a site login, or us
        } catch APIError.siteLogin(let realm) {
            phase = .siteLogin(realm: realm)
            return
        } catch {}
        guard client.hasSessionCookie else { phase = .signedOut; return }
        do {
            signedIn(try await client.me(), as: "session resumed")
        } catch APIError.signedOut {
            phase = .signedOut
        } catch {
            // Offline, or the server is restarting: keep the session and try again, rather than ask for a password.
            offline = true
            phase = .unreachable
            retrying?.cancel()
            retrying = Task { [weak self] in
                try? await Task.sleep(for: .seconds(4))
                guard let self, !Task.isCancelled, self.phase == .unreachable else { return }
                await self.start()
            }
        }
    }

    /// The private server's site login, entered once and kept in the app's settings.
    public func useSiteLogin(user: String, password: String) async throws {
        guard case .siteLogin = phase else { return }
        let url = client.baseURL
        let login = APIClient.basicAuthorization(user: user, password: password)
        let candidate = APIClient(baseURL: url, cookies: cookies, siteLogin: login)
        // Kept only once the server has taken it: past the gate, the API answers (a 401 means just "not signed in").
        do {
            _ = try await candidate.threads()
        } catch APIError.siteLogin {
            throw APIError.server(status: 401, detail: "that site login didn't work")
        } catch APIError.signedOut {}
        client = candidate
        var logins = Self.siteLogins(defaults)
        logins[url.absoluteString] = login
        defaults.set(logins, forKey: "siteLogins")
        await start()
    }

    /// From the "can't reach Sammy" screen: try now.
    public func retryNow() {
        retrying?.cancel()
        Task { await start() }
    }

    public func signIn(email: String, password: String) async throws {
        let started = Date()
        signedIn(try await client.signIn(email: email.trimmingCharacters(in: .whitespaces), password: password), as: "sign in", started: started)
    }

    public func signUp(email: String, password: String) async throws {
        let started = Date()
        signedIn(try await client.signUp(email: email.trimmingCharacters(in: .whitespaces), password: password), as: "sign up", started: started)
    }

    public func requestPasswordReset(email: String) async throws {
        try await client.requestPasswordReset(email: email.trimmingCharacters(in: .whitespaces))
    }

    public func resetPassword(email: String, code: String, password: String) async throws {
        let started = Date()
        signedIn(try await client.resetPassword(email: email.trimmingCharacters(in: .whitespaces),
                                                code: code.trimmingCharacters(in: .whitespaces), password: password),
                 as: "reset password", started: started)
    }

    public func signOut() async {
        await finishPendingDelete()  // while the session can still delete it
        // Signing out on purpose forgets what was kept for next time; a session that ends by itself keeps it.
        signOuts += 1
        if let id = user?.id {
            persistedUser = nil  // nothing typed or opened while signing out is kept again
            defaults.removeObject(forKey: "drafts.\(id)")
            defaults.removeObject(forKey: "route.\(id)")
            defaults.removeObject(forKey: "unseen.\(id)")
            defaults.removeObject(forKey: "active.\(id)")
        }
        // Said, and sent, while the session still lets the server take it.
        telemetry.log("sign out")
        await telemetry.shutdown()
        try? await client.signOut()
        client.clearSession()
        reset()
        signedOutReason = nil
        phase = .signedOut
    }

    /// Everything of the user's, as a zip, or word that a link to it is on its way by email (a big account).
    public func exportData() async throws -> DataExport {
        telemetry.log("export data")
        return try await client.exportData()
    }

    /// Deletes the account and everything of it on the server, the user's password confirming it, then forgets it
    /// here as signing out does. Throws, and changes nothing, when the password is wrong.
    public func deleteAccount(password: String) async throws {
        await finishPendingDelete()  // while the session can still delete it
        telemetry.log("delete account")
        try await client.deleteAccount(password: password)
        await signOut()
    }

    /// The server said the session is over (expired, or signed out elsewhere).
    public func sessionEnded() {
        guard user != nil else { return }
        telemetry.log("session ended")  // sent only if the server still takes it
        let telemetry = telemetry
        Task { await telemetry.shutdown() }
        client.clearSession()
        reset()
        signedOutReason = "Your session ended. Sign in again to carry on."
        phase = .signedOut
    }

    /// Talk to another server (signed out of this one).
    public func useServer(_ url: URL) {
        guard url != client.baseURL else { return }
        if let thread = recentlyDeleted {  // on the server it was deleted from, while signed in there
            pendingDelete?.cancel()
            let old = client
            Task { try? await old.delete(thread: thread.id) }
        }
        let telemetry = telemetry
        Task { await telemetry.shutdown() }
        reset()
        defaults.set(url.absoluteString, forKey: "serverURL")
        client = APIClient(baseURL: url, cookies: cookies, siteLogin: Self.siteLogins(defaults)[url.absoluteString])
        // As at launch: a private server asks for its site login first, and one that can't be reached says so.
        phase = .launching
        Task { await start() }
    }

    /// `how` names the span telemetry gets for it, from `started`: it is only on (or off) once signed in.
    private func signedIn(_ user: User, as how: String, started: Date = Date()) {
        retrying?.cancel()
        offline = false
        signedOutReason = nil
        phase = .signedIn(user)
        // Back where the user was, with what they were writing: a chat deleted meanwhile becomes a new task.
        drafts = defaults.dictionary(forKey: "drafts.\(user.id)") as? [String: String] ?? [:]
        unseen = Set(defaults.stringArray(forKey: "unseen.\(user.id)") ?? [])
        pinned = defaults.stringArray(forKey: "pinned.\(user.id)") ?? []
        squirrelName = defaults.string(forKey: "squirrelName.\(user.id)") ?? ""
        persistedUser = user.id
        open(defaults.string(forKey: "route.\(user.id)").flatMap(Route.init(stored:)) ?? .chat(nil))
        startWatching()
        updateTunnel()
        let client = client
        let launch = launchLogged ? nil : launched
        launchLogged = true
        Task {
            await client.startTelemetry(userId: user.id)
            if let launch { client.telemetry.log("app launch", ["sammy.signed_in": .string(how)], start: launch) }
            client.telemetry.log(how, start: started)
        }
    }

    // MARK: the Mac tunnel

    /// Open while signed in with `browseFromMac` on; closed otherwise. Turning it back on takes the tunnel back from
    /// another Mac that took it over.
    private func updateTunnel() {
        guard browseFromMac, user != nil else { return stopTunnel() }
        guard tunnel == nil, let request = client.tunnelSocketRequest() else { return }
        tunnels += 1
        let opened = tunnels
        let tunnel = MacTunnel(request: request, session: client.session) { [weak self] status in
            Task { @MainActor in
                guard let self, self.tunnels == opened, self.tunnel != nil else { return }
                self.tunnelStatus = status
            }
        }
        self.tunnel = tunnel
        Task { await tunnel.start() }
    }

    private func stopTunnel() {
        guard let tunnel else { return }
        self.tunnel = nil
        tunnelStatus = MacTunnel.Status()
        Task { await tunnel.stop() }
    }

    /// The open chat's summary, as the chat list has it.
    public var openThread: ThreadSummary? {
        guard case .chat(let id?) = route else { return nil }
        return threads.first { $0.id == id } ?? chat.flatMap { $0.threadId == id ? ThreadSummary(id: id, title: $0.title) : nil }
    }

    private func reset() {
        persistedUser = nil  // before clearing: what is kept for next time stays
        stopTunnel()
        signedOutOfNotifications?()
        retrying?.cancel()
        watching?.cancel()
        watching = nil
        chat?.close()
        chat = nil
        threads = []
        threadsLoaded = false
        known = [:]
        notified = []
        drafts = [:]
        draftAttachments = [:]
        attachmentCache = [:]
        attachmentCacheBytes = 0
        answerDrafts = [:]
        deleting = nil
        renaming = nil
        showingPalette = false
        wantsFindChats = false
        actionError = nil
        pendingDelete?.cancel()
        recentlyDeleted = nil
        backStack = []
        forwardStack = []
        unseen = []
        pinned = []
        squirrelName = ""
        schedules = nil
        savedSites = nil
        memories = nil
        integrations = nil
        apps = nil
        serverNote = nil
        route = .chat(nil)
        libraryError = nil
        threadsChanged?()
    }

    /// Who is signed in, for a chat to know whose it is.
    var userId: String? { user?.id }
    /// Counts deliberate sign-outs: text from a chat opened before one is not kept (a session that merely ended is not
    /// one, so what the user wrote then waits for them).
    private(set) var signOuts = 0

    /// A message from a chat that closed before it could send: keep it as that chat's draft, and show it if the
    /// chat is open again. It is `owner`'s: if they are not the one signed in now (the session ended meanwhile), it
    /// waits in their saved drafts for their next sign-in.
    func keepDraft(_ text: String, for key: String, owner: String?, signOuts: Int) {
        guard signOuts == self.signOuts else { return }  // signed out on purpose since: forgotten, as promised
        func joined(_ current: String?) -> String {
            let current = (current ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            return current.isEmpty ? text : text + "\n\n" + current
        }
        guard let owner, owner == persistedUser else {
            guard let owner else { return }
            var saved = defaults.dictionary(forKey: "drafts.\(owner)") as? [String: String] ?? [:]
            saved[key] = joined(saved[key])
            defaults.set(saved, forKey: "drafts.\(owner)")
            return
        }
        let kept = joined(drafts[key])
        drafts[key] = kept
        if let chat, (chat.threadId ?? "new") == key { chat.draft = kept }
    }

    // MARK: navigation

    public func open(_ route: Route) {
        // Mid sign-in, the browser stays on screen: a shortcut, a menu or a notification must not abandon it, and
        // says why nothing happened.
        if isTakingOver, route != self.route {
            chat?.live?.say("Finish signing in and choose I'm done, or Not now (⇧⌘T), to go elsewhere.")
            return
        }
        if route != self.route, !travelling {
            backStack.append(self.route)
            if backStack.count > 50 { backStack.removeFirst() }
            forwardStack.removeAll()
        }
        libraryError = nil
        if case .chat(let id) = route, let chat, chat.threadId == id, id != nil {
            self.route = route
            return
        }
        if case .chat(nil) = route, let chat, chat.threadId == nil {
            self.route = route
            return
        }
        chat?.close()
        chat = nil
        self.route = route
        seeOpenChat()
        switch route {
        case .chat(let id):
            let title = threads.first(where: { $0.id == id })?.title ?? ""
            let model = ChatModel(app: self, threadId: id, title: title)
            chat = model
            if let id { Task { await telemetry.action("open chat", ["sammy.thread_id": .string(id)]) { _ in await model.load() } } }
        case .schedules: Task { await telemetry.action("open schedules") { _ in await loadSchedules() } }
        case .signIns: Task { await telemetry.action("open saved sign-ins") { _ in await loadSavedSites() } }
        case .integrations: Task { await telemetry.action("open integrations") { _ in await loadIntegrations() } }
        case .memory: Task { await telemetry.action("open memory") { _ in await loadMemories() } }
        }
    }

    /// The user is looking at the open chat (the app in front, its window on screen): it is seen.
    private func seeOpenChat() {
        guard isActive, isWindowVisible, case .chat(let id?) = route else { return }
        unseen.remove(id)
        chatSeen?(id)
    }

    /// An answer typed in a notification. True if Sammy took it; otherwise it is kept with its chat, which opens with
    /// it in the answer box (or the message box, if the question closed meanwhile).
    public func answerFromNotification(_ askId: String, in threadId: String, text: String) async -> Bool {
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, user != nil else { return false }  // the caller checks it is this user's
        do {
            try await client.answer(askId, .text(text))
            refreshThreads()
            if chat?.threadId == threadId { await chat?.refresh() }
            return true
        } catch APIError.signedOut {
            sessionEnded()
        } catch {
            answerDrafts[askId] = AnswerDraft(threadId: threadId, text: text)
            if chat?.threadId == threadId { await chat?.refresh() }  // where it can go now, with a word on why
        }
        return false
    }

    /// The chats whose contents mention `query` (titles are matched here too, without asking); none if the server
    /// can't say.
    public func search(_ query: String) async -> Set<String> {
        let query = query.trimmingCharacters(in: .whitespaces)
        guard query.count >= 2, let ids = try? await client.search(query) else { return [] }
        return Set(ids)
    }

    /// Marks a chat as not seen yet, to come back to it, as Mail's Mark as Unread.
    public func markUnread(_ thread: ThreadSummary) {
        unseen.insert(thread.id)
    }

    public func markSeen(_ thread: ThreadSummary) {
        unseen.remove(thread.id)
        chatSeen?(thread.id)
    }

    public func setPinned(_ thread: ThreadSummary, _ pin: Bool) {
        pinned.removeAll { $0 == thread.id }
        if pin { pinned.append(thread.id) }
    }

    /// Names the squirrel: trimmed, and at most `squirrelNameLimit` characters. Empty forgets the name.
    public func nameSquirrel(_ name: String) {
        squirrelName = String(name.trimmingCharacters(in: .whitespacesAndNewlines).prefix(Self.squirrelNameLimit))
    }

    public var canGoBack: Bool { !backStack.isEmpty }
    public var canGoForward: Bool { !forwardStack.isEmpty }

    public func goBack() { travel(from: &backStack, to: &forwardStack) }
    public func goForward() { travel(from: &forwardStack, to: &backStack) }

    /// Opens the latest place in `from` that is still there (a chat deleted since is skipped), keeping where the user
    /// is in `to`.
    private func travel(from: inout [Route], to: inout [Route]) {
        guard !isTakingOver else { return }
        while let route = from.popLast() {
            if route == self.route { continue }  // already here: nothing would happen
            if case .chat(let id?) = route, !threads.contains(where: { $0.id == id }) { continue }
            to.append(self.route)
            travelling = true
            open(route)
            travelling = false
            return
        }
    }

    /// Reads the open page again: on coming back to the app, and from a page that couldn't load.
    public func reloadPage() {
        switch route {
        case .chat: Task { await chat?.refresh() }
        case .schedules: Task { await loadSchedules() }
        case .signIns: Task { await loadSavedSites() }
        case .integrations: Task { await loadIntegrations() }
        case .memory: Task { await loadMemories() }
        }
    }

    func chatCreated(_ chat: ChatModel) {
        guard self.chat === chat, let id = chat.threadId else { return }
        route = .chat(id)
        if !threads.contains(where: { $0.id == id }) {
            threads.insert(ThreadSummary(id: id, title: chat.title, status: .queued, updatedAt: Date()), at: 0)
            known[id] = .some(.queued)  // so its first question notifies, even before the list is read again
            localChanges += 1
            threadsChanged?()
        }
    }

    func chatVanished(_ id: String) {
        drafts[id] = nil
        draftAttachments[id] = nil
        unseen.remove(id)
        pinned.removeAll { $0 == id }
        chatSeen?(id)
        threads.removeAll { $0.id == id }
        localChanges += 1
        threadsChanged?()
        if chat?.threadId == id { open(.chat(nil)) }
    }

    /// The open chat's run changed: keep its row in the list in step without waiting for the next read. A change
    /// the user saw happen (the app in front, this chat on screen) needs no notification; one they did not see is
    /// left for the next read of the list to notice.
    func chatChanged(_ chat: ChatModel) {
        guard let id = chat.threadId, let index = threads.firstIndex(where: { $0.id == id }) else { return }
        let status = chat.run?.status.isActive == true ? chat.run?.status : nil
        if isActive, isWindowVisible, self.chat === chat { known[id] = .some(status) }
        if threads[index].status != status || (status == .waiting && chat.ask != nil && threads[index].waitingFor != chat.ask?.kind) {
            let outcome = status == nil ? chat.run?.status : nil
            let title = chat.title.isEmpty ? threads[index].title : chat.title
            if status != nil, threads[index].status == nil {
                // A new task in an old chat: it is the latest now, at the top, as the next read will have it.
                threads.remove(at: index)
                threads.insert(ThreadSummary(id: id, title: title, status: status, updatedAt: Date(), waitingFor: chat.ask?.kind), at: 0)
            } else {
                threads[index] = threads[index].with(title: title, status: status, outcome: outcome, waitingFor: chat.ask?.kind)
            }
            localChanges += 1
            threadsChanged?()
        }
    }

    func taskSent() {
        if !defaults.bool(forKey: "sentFirstTask") {
            defaults.set(true, forKey: "sentFirstTask")
            firstTaskSent?()
        }
    }

    // MARK: the chat list, and noticing what changed

    public func refreshThreads() {
        Task { await loadThreads() }
    }

    /// One read at a time; a read asked for meanwhile runs once the current one is done, so an older answer never
    /// overwrites a newer one.
    public func loadThreads() async {
        guard user != nil else { return }
        if loadingThreads {  // the running read goes round once more; wait for it
            loadAgain = true
            while loadingThreads { try? await Task.sleep(for: .milliseconds(20)) }
            return
        }
        loadingThreads = true
        defer { loadingThreads = false }
        repeat {
            loadAgain = false
            let changes = localChanges
            do {
                // A chat deleted a moment ago stays off the list, though the server still has it.
                let fresh = try await client.threads().filter { $0.id != recentlyDeleted?.id && deletesInFlight[$0.id] == nil }
                guard user != nil else { return }
                if localChanges != changes { loadAgain = true; continue }  // changed here meanwhile: read again
                offline = false
                let first = !threadsLoaded
                threads = fresh
                threadsLoaded = true
                threadsChanged?()
                await notice(changesIn: fresh, quietly: first)
            } catch APIError.signedOut {
                sessionEnded()
                return
            } catch APIError.offline {
                offline = true
            } catch {}
        } while loadAgain
    }

    private func startWatching() {
        watching?.cancel()
        watching = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                // Polling: no spans of its own, and not part of the sign-in that started it.
                await Telemetry.$parent.withValue(nil) { await Telemetry.$quiet.withValue(true) { await self.loadThreads() } }
                // Quick while Sammy works, so a question or a finished task is noticed within seconds; calmer while
                // chats only wait for the user.
                let working = self.threads.contains { $0.status?.isWorking == true }
                let waiting = self.threads.contains { $0.status == .waiting }
                try? await Task.sleep(for: .seconds(working ? 3 : waiting ? 10 : 20))
            }
        }
    }

    private func notice(changesIn fresh: [ThreadSummary], quietly: Bool) async {
        var changed: [(ThreadSummary, RunStatus?)] = []
        for thread in fresh {
            let before = known[thread.id] ?? nil
            // A chat first seen now (a schedule's, or one from another device) counts as new: it may already wait.
            if known[thread.id] != nil ? before != thread.status : thread.status == .waiting { changed.append((thread, before)) }
            known[thread.id] = thread.status
        }
        // A task that ended while the user looked elsewhere is new to them, notified or not; at launch, one that was
        // going on when the app last looked and has ended since, while it was closed.
        let looking = { (id: String) in self.isActive && self.isWindowVisible && self.chat?.threadId == id }
        if quietly, let user = persistedUser {
            let wasActive = Set(defaults.stringArray(forKey: "active.\(user)") ?? [])
            for thread in fresh where thread.status == nil && wasActive.contains(thread.id) && !looking(thread.id) {
                unseen.insert(thread.id)
            }
        } else if !quietly {
            for (thread, before) in changed where thread.status == nil && before?.isActive == true && !looking(thread.id) {
                unseen.insert(thread.id)
            }
        }
        // Deleted elsewhere; a chat deleted here a moment ago keeps its marks until it is gone for good (undo).
        let ids = Set(fresh.map(\.id) + [recentlyDeleted?.id].compactMap { $0 })
        unseen.formIntersection(ids)
        if pinned.contains(where: { !ids.contains($0) }) { pinned.removeAll { !ids.contains($0) } }
        if let user = persistedUser {
            defaults.set(fresh.filter { $0.status?.isActive == true }.map(\.id), forKey: "active.\(user)")
        }
        // Until the user has been told, a change is not known: if telling fails (a blip reading the chat), the
        // next read of the list tries again.
        func untold(_ thread: ThreadSummary, _ before: RunStatus?) { known[thread.id] = .some(before) }
        guard !quietly, let notify else { return }
        for (thread, before) in changed {
            let looking = isActive && isWindowVisible && chat?.threadId == thread.id
            if thread.status == .waiting, !looking {
                guard let detail = try? await client.thread(thread.id) else { untold(thread, before); continue }
                guard let ask = detail.run?.ask, notified.insert(ask.id).inserted else { continue }
                let kind = Notice.Kind(rawValue: ask.kind.rawValue) ?? .approval  // a kind not known yet: no reply box
                let notice = Notice(kind: kind, threadId: thread.id, title: Self.headline(for: ask.kind, from: sammyName), body: ask.prompt, askId: ask.id)
                notify(notice)
                noticePosted(notice, run: detail.run?.id, ask: ask.id)
            } else if thread.status == nil, before?.isActive == true, !looking {
                guard let detail = try? await client.thread(thread.id) else { untold(thread, before); continue }
                guard let run = detail.run else { continue }
                let notice: Notice
                switch run.status {
                case .done:
                    let reply = run.output ?? detail.messages.last(where: { $0.role == .assistant })?.text ?? "\(sammyName) finished."
                    notice = Notice(kind: .finished, threadId: thread.id, title: thread.title.readableTitle, body: Self.plain(reply))
                case .failed:
                    notice = Notice(kind: .finished, threadId: thread.id, title: thread.title.readableTitle, body: "\(sammyName) couldn't finish this task.")
                default:
                    continue  // stopped: the user did that themselves
                }
                notify(notice)
                noticePosted(notice, run: run.id, status: run.status)
            }
        }
    }

    private func noticePosted(_ notice: Notice, run: String?, ask: String? = nil, status: RunStatus? = nil) {
        telemetry.log("notification posted", [
            "sammy.notice.kind": .string(notice.kind.rawValue), "sammy.thread_id": .string(notice.threadId),
            "sammy.run_id": run.map { .string($0) }, "sammy.ask_id": ask.map { .string($0) },
            "sammy.run.status": status.map { .string($0.rawValue) },
        ]) {
            $0.content("sammy.notice.title", notice.title)
            $0.content("sammy.notice.body", notice.body)
        }
    }

    /// The title of an ask: what `sammy` (`sammyName`) wants from the user.
    public nonisolated static func headline(for kind: AskKind, from sammy: String) -> String {
        switch kind {
        case .question: return "\(sammy) has a question"
        case .approval: return "\(sammy) needs your OK"
        case .handoff: return "\(sammy) needs you in the browser"
        case .connect: return "\(sammy) needs an app connected"
        case .other: return "\(sammy) needs you"
        }
    }

    /// A reply as one plain line for a notification: no Markdown marks, at most 200 characters.
    nonisolated static func plain(_ text: String) -> String {
        let stripped = text
            .replacingOccurrences(of: #"\[([^\]]+)\]\([^)]+\)"#, with: "$1", options: .regularExpression)
            .replacingOccurrences(of: #"(?m)^\s*(#{1,6}|>|[-*+])\s+"#, with: "", options: .regularExpression)
            .replacingOccurrences(of: #"\*\*|__|`"#, with: "", options: .regularExpression)
            .replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespaces)
        return stripped.count > 200 ? String(stripped.prefix(199)) + "…" : stripped
    }

    // MARK: managing chats

    public func rename(_ thread: ThreadSummary, to title: String) async {
        let title = title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty, title != thread.title else { return }
        let renamed = await telemetry.action("rename chat", ["sammy.thread_id": .string(thread.id)]) { span in
            span.content("sammy.title", title)
            return await act("Couldn't rename the chat", { try await self.client.rename(thread: thread.id, to: title) }) != nil
        }
        if renamed {
            if let index = threads.firstIndex(where: { $0.id == thread.id }) {
                threads[index] = threads[index].with(title: title, status: threads[index].status, outcome: threads[index].outcome)
            }
            chat?.renamed(thread.id, to: title)
            localChanges += 1
            threadsChanged?()
        }
    }

    /// Deletes the chat with a moment to undo it: off the list now, from the server once the moment passes. A chat
    /// deleted before still waiting for its moment is deleted now.
    public func deleteWithUndo(_ thread: ThreadSummary) {
        if let previous = recentlyDeleted {
            pendingDelete?.cancel()
            commit(previous)
        }
        let index = threads.firstIndex { $0.id == thread.id } ?? 0
        deletedFrom = (index, chat?.threadId == thread.id)
        threads.removeAll { $0.id == thread.id }
        localChanges += 1
        threadsChanged?()
        if deletedFrom.wasOpen { open(.chat(nil)) }
        recentlyDeleted = thread
        let window = undoWindow
        pendingDelete = Task { [weak self] in
            try? await Task.sleep(for: window)
            guard !Task.isCancelled, let self, self.recentlyDeleted?.id == thread.id else { return }
            self.pendingDelete = nil
            self.recentlyDeleted = nil
            await self.commit(thread).value
        }
    }

    /// Puts the chat deleted a moment ago back where it was.
    public func undoDelete() {
        guard let thread = recentlyDeleted else { return }
        pendingDelete?.cancel()
        recentlyDeleted = nil
        if !threads.contains(where: { $0.id == thread.id }) {
            threads.insert(thread, at: min(deletedFrom.index, threads.count))
            localChanges += 1
            threadsChanged?()
        }
        if deletedFrom.wasOpen { open(.chat(thread.id)) }
    }

    /// Deletes the chat waiting for its moment now, and waits for every deletion on its way: before signing out or
    /// quitting.
    public func finishPendingDelete() async {
        if let thread = recentlyDeleted {
            pendingDelete?.cancel()
            recentlyDeleted = nil
            commit(thread)
        }
        for task in Array(deletesInFlight.values) { await task.value }
    }

    /// Sends the deletion, in a task of its own (not one that undo or a new deletion cancels), tracked until answered.
    @discardableResult
    private func commit(_ thread: ThreadSummary) -> Task<Void, Never> {
        let task = Task { [weak self] in
            await self?.delete(thread)
            self?.deletesInFlight[thread.id] = nil
        }
        deletesInFlight[thread.id] = task
        return task
    }

    /// Deletes the chat; if it is the one on screen, a new task takes its place.
    public func delete(_ thread: ThreadSummary) async {
        let deleted = await telemetry.action("delete chat", ["sammy.thread_id": .string(thread.id)]) { _ in
            do {
                try await client.delete(thread: thread.id)
            } catch APIError.server(status: 404, _) {
                // Deleted elsewhere already: gone either way.
            } catch APIError.signedOut {
                sessionEnded()
                return false
            } catch {
                actionError = "Couldn't delete the chat. \(error.localizedDescription)"
                return false
            }
            return true
        }
        guard deleted else { return }
        answerDrafts = answerDrafts.filter { $0.value.threadId != thread.id }
        chatVanished(thread.id)
    }

    // MARK: schedules, saved sign-ins, memory

    public func loadSchedules() async { schedules = await library { try await self.client.schedules() } ?? schedules }
    public func loadSavedSites() async { savedSites = await library { try await self.client.savedSites() } ?? savedSites }
    public func loadMemories() async { memories = await library { try await self.client.memories() } ?? memories }

    public func setPaused(_ schedule: Schedule, _ paused: Bool) async {
        let updated = await telemetry.action(paused ? "pause schedule" : "resume schedule", ["sammy.schedule_id": .string(schedule.id)]) { _ in
            await library({ try await self.client.setPaused(schedule.id, paused) })
        }
        if let updated {
            schedules = schedules?.map { $0.id == updated.id ? updated : $0 }
        }
    }

    public func delete(_ schedule: Schedule) async {
        let deleted = await telemetry.action("delete schedule", ["sammy.schedule_id": .string(schedule.id)]) { _ in
            await library({ try await self.client.deleteSchedule(schedule.id) }) != nil
        }
        if deleted {
            schedules?.removeAll { $0.id == schedule.id }
        }
    }

    public func forget(_ site: SavedSite) async {
        let forgotten = await telemetry.action("forget sign-in", ["sammy.site": .string(site.site)]) { _ in
            await library({ try await self.client.forget(site: site.site) }) != nil
        }
        if forgotten {
            savedSites?.removeAll { $0.site == site.site }
        }
    }

    public func forget(_ memory: Memory) async {
        let forgotten = await telemetry.action("forget memory", ["sammy.memory_id": .string(memory.id)]) { _ in
            await library({ try await self.client.deleteMemory(memory.id) }) != nil
        }
        if forgotten {
            memories?.removeAll { $0.id == memory.id }
        }
    }

    // MARK: integrations

    public func loadIntegrations() async {
        integrations = await library { try await self.client.integrations() } ?? integrations
        if integrations != nil, apps == nil {  // listed MCP servers (PostHog) are there without Composio too
            apps = await library { try await self.client.apps() }
        }
    }

    /// Opens where the user signs in to the app; the page updates when they come back to Sammy.
    public func connect(app slug: String) async {
        let url = await telemetry.action("connect app", ["sammy.app": .string(slug)]) { _ in
            await library { try await self.client.connect(app: slug) }
        }
        if let url { openInBrowser?(url) }
    }

    /// Opens where the user signs in to their MCP server again.
    public func signIn(server: String) async {
        let url = await telemetry.action("sign in to mcp server") { _ in
            await library { try await self.client.signIn(server: server) }
        }
        if let url { openInBrowser?(url) }
    }

    public func remove(_ connection: Connection) async {
        let removed = await telemetry.action("remove integration", ["sammy.integration.provider": .string(connection.provider)]) { _ in
            await library {
                if connection.isApp { try await self.client.disconnect(app: connection.id) } else { try await self.client.remove(server: connection.id) }
            } != nil
        }
        if removed { await loadIntegrations() }
    }

    /// Adds the user's MCP server. True once the server has it; if it wants a sign-in, that opens in the browser.
    /// The address and header are never in telemetry: either can hold a key.
    public func addServer(name: String, url: String, header: String, value: String) async -> Bool {
        let header = header.trimmingCharacters(in: .whitespaces)
        let headers = header.isEmpty ? [:] : [header: value]
        let added = await addServer(
            name: name.trimmingCharacters(in: .whitespaces), url: url.trimmingCharacters(in: .whitespaces), headers: headers,
            ["sammy.with_header": .bool(!header.isEmpty)]
        )
        guard added != nil else { return false }
        serverName = ""
        return true
    }

    /// Adds a known MCP server, such as PostHog's, as the user would by hand: one that signs in with OAuth opens its
    /// sign-in in the browser; one that takes a token (GitHub's) sends the `token` the user pasted. True once added.
    /// The token is never in telemetry.
    @discardableResult
    public func connect(preset: CatalogApp, token: String? = nil) async -> Bool {
        guard let url = preset.url else { return false }
        let headers = preset.needsToken ? Self.tokenHeaders(preset.tokenHeader, token) : [:]
        if preset.needsToken && headers.isEmpty { return false }
        return await addServer(name: preset.name, url: url, headers: headers, ["sammy.preset": .string(preset.key)]) != nil
    }

    /// The header a known MCP server's token goes in, as `Bearer <token>`; none without a token.
    nonisolated static func tokenHeaders(_ header: String?, _ token: String?) -> [String: String] {
        let token = (token ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return token.isEmpty ? [:] : [header ?? "Authorization": "Bearer \(token)"]
    }

    /// Adds an MCP server and opens its sign-in, if it wants one; `serverNote` says how that went (a name already
    /// taken, say). The page reads itself again after.
    @discardableResult
    func addServer(name: String, url: String, headers: [String: String], _ attributes: [String: AttributeValue?]) async -> AddedServer? {
        let added: AddedServer? = await telemetry.action("add mcp server", attributes) { _ in
            do {
                return try await self.client.addServer(name: name, url: url, headers: headers)
            } catch APIError.signedOut {
                self.sessionEnded()
            } catch {
                self.serverNote = .error(error.localizedDescription)
            }
            return nil
        }
        guard let added else { return nil }
        if let link = added.signInUrl, let url = try? APIClient.url(link) {
            serverNote = .info("Sign in to \(added.connection.name) in your browser to finish.")
            openInBrowser?(url)
        } else {
            serverNote = .info("\(added.connection.name) is connected.")
        }
        await loadIntegrations()
        return added
    }

    // MARK: files in chats

    /// A file in a chat, to open or save: from memory if it was fetched (or uploaded) already. Its name stays on the
    /// Mac; its size may go.
    public func download(_ file: Attachment) async -> Data? {
        if let data = attachmentCache[file.id] { return data }
        let data = await telemetry.action("download attachment", ["sammy.file.size": .int(file.size)]) { _ in
            await act("Couldn't get the file") { try await self.client.attachment(id: file.id) }
        }
        if let data { remember(data, for: file) }
        return data
    }

    /// An image in a chat, for its thumbnail: quietly (no span, nothing said if it fails, as for any picture that
    /// doesn't load).
    public func picture(_ file: Attachment) async -> Data? {
        if let data = attachmentCache[file.id] { return data }
        let data = try? await Telemetry.$quiet.withValue(true) { try await client.attachment(id: file.id) }
        if let data { remember(data, for: file) }
        return data
    }

    /// Keeps a file's bytes in memory, up to a limit: past it, everything kept is forgotten (the simplest bound; a
    /// picture still on screen stays drawn, and anything else is fetched again when asked for).
    func remember(_ data: Data, for file: Attachment) {
        guard attachmentCache[file.id] == nil, data.count <= Self.attachmentCacheLimit / 4 else { return }
        if attachmentCacheBytes + data.count > Self.attachmentCacheLimit {
            attachmentCache = [:]
            attachmentCacheBytes = 0
        }
        attachmentCache[file.id] = data
        attachmentCacheBytes += data.count
    }

    static let attachmentCacheLimit = 128 * 1024 * 1024

    /// An upload finished (or failed): its chip says so, in whichever chat's draft it is now.
    func attachmentChanged(_ id: UUID, to state: PendingAttachment.State) {
        for (key, files) in draftAttachments {
            guard let index = files.firstIndex(where: { $0.id == id }) else { continue }
            draftAttachments[key]?[index].state = state
            return
        }
    }

    /// Does something the user asked for to a chat; if it fails, says so with `what` went wrong.
    private func act<T>(_ what: String, _ call: () async throws -> T) async -> T? {
        do {
            return try await call()
        } catch APIError.signedOut {
            sessionEnded()
        } catch {
            actionError = "\(what). \(error.localizedDescription)"
        }
        return nil
    }

    private func library<T>(_ call: () async throws -> T) async -> T? {
        do {
            let result = try await call()
            libraryError = nil
            return result
        } catch APIError.signedOut {
            sessionEnded()
        } catch let error as APIError {
            libraryError = error.localizedDescription
            telemetry.shown(error, libraryError, ["sammy.route": .string(String(describing: route).components(separatedBy: "(").first ?? "")])
        } catch {}
        return nil
    }
}
