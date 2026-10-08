import Foundation
import Network

/// The Mac tunnel's wire, as the server speaks it (`montybot/browser/tunnel.py`): binary WebSocket messages,
/// `kind (1 byte) | stream id (4 bytes, big-endian) | payload`.
public enum TunnelWire {
    public static let open: UInt8 = 1, opened: UInt8 = 2, data: UInt8 = 3, close: UInt8 = 4
    /// `OPENED`'s answers, SOCKS5 statuses, which the server passes on to the browser.
    public static let succeeded: UInt8 = 0, notAllowed: UInt8 = 2, unreachable: UInt8 = 4, refused: UInt8 = 5
    /// The only ports the tunnel carries: the web's.
    public static let webPorts: Set<UInt16> = [80, 443]
    /// The close code when another Mac of the same user took the tunnel over: not to be taken straight back.
    public static let replaced = 4000

    public struct Message: Equatable, Sendable {
        public let kind: UInt8
        public let stream: UInt32
        public let payload: Data
    }

    /// Where `OPEN` asks the Mac to connect.
    public struct Target: Equatable, Sendable {
        public let host: String
        public let port: UInt16
    }

    public static func message(_ kind: UInt8, _ stream: UInt32, _ payload: Data = Data()) -> Data {
        var data = Data([kind])
        withUnsafeBytes(of: stream.bigEndian) { data.append(contentsOf: $0) }
        return data + payload
    }

    public static func parse(_ data: Data) -> Message? {
        let bytes = [UInt8](data)
        guard bytes.count >= 5 else { return nil }
        let stream = bytes[1..<5].reduce(UInt32(0)) { $0 << 8 | UInt32($1) }
        return Message(kind: bytes[0], stream: stream, payload: Data(bytes[5...]))
    }

    /// `OPEN`'s payload: the port, then the host name in ASCII.
    public static func target(_ payload: Data) -> Target? {
        let bytes = [UInt8](payload)
        guard bytes.count > 2, let host = String(bytes: bytes[2...], encoding: .ascii) else { return nil }
        return Target(host: host, port: UInt16(bytes[0]) << 8 | UInt16(bytes[1]))
    }
}

/// Which addresses the tunnel may connect to: the public internet's only. Never this Mac, the user's network, or
/// anything special-purpose, so a web page the server's browser opens can't reach the user's router or printer.
public enum PublicAddress {
    /// `bytes` is an IPv4 (4 bytes) or IPv6 (16 bytes) address.
    public static func allows(_ bytes: [UInt8]) -> Bool {
        switch bytes.count {
        case 4: return allowsIPv4(bytes)
        case 16: return allowsIPv6(bytes)
        default: return false
        }
    }

    private static func allowsIPv4(_ b: [UInt8]) -> Bool {
        switch (b[0], b[1], b[2]) {
        case (0, _, _), (10, _, _), (127, _, _), (224..., _, _): false  // this network, private, loopback, multicast+
        case (100, 64...127, _): false  // carrier-grade NAT
        case (169, 254, _): false  // link-local
        case (172, 16...31, _): false
        case (192, 168, _): false
        case (192, 0, 0), (192, 0, 2), (198, 51, 100), (203, 0, 113): false  // protocol assignments, documentation
        case (198, 18...19, _): false  // benchmarking
        default: true
        }
    }

    /// Global unicast (2000::/3) only, without documentation, 6to4 and Teredo, which can carry any IPv4 address.
    /// An IPv4 address mapped into IPv6 is judged as IPv4.
    private static func allowsIPv6(_ b: [UInt8]) -> Bool {
        if b[0..<10].allSatisfy({ $0 == 0 }) && b[10] == 0xff && b[11] == 0xff { return allowsIPv4(Array(b[12...])) }
        guard b[0] & 0xe0 == 0x20 else { return false }
        switch (b[0], b[1], b[2], b[3]) {
        case (0x20, 0x01, 0x0d, 0xb8), (0x20, 0x01, 0x00, 0x00), (0x20, 0x02, _, _): return false
        default: return true
        }
    }

    /// Every address `host` resolves to, as bytes; empty when it does not resolve. Blocks: call it off the actor.
    static func resolve(_ host: String) -> [[UInt8]] {
        var hints = addrinfo()
        hints.ai_family = AF_UNSPEC
        hints.ai_socktype = SOCK_STREAM
        var list: UnsafeMutablePointer<addrinfo>?
        guard getaddrinfo(host, nil, &hints, &list) == 0, let first = list else { return [] }
        defer { freeaddrinfo(first) }
        var addresses: [[UInt8]] = []
        for info in sequence(first: first, next: { $0.pointee.ai_next }) {
            guard let address = info.pointee.ai_addr else { continue }
            switch Int32(address.pointee.sa_family) {
            case AF_INET:
                address.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { address in
                    withUnsafeBytes(of: address.pointee.sin_addr) { addresses.append(Array($0)) }
                }
            case AF_INET6:
                address.withMemoryRebound(to: sockaddr_in6.self, capacity: 1) { address in
                    withUnsafeBytes(of: address.pointee.sin6_addr) { addresses.append(Array($0)) }
                }
            default: break
            }
        }
        return addresses
    }
}

/// The Mac tunnel: while it is open, the server's browser for a task the user started goes out to the web from this
/// Mac, so sites see the user's own address rather than a datacenter's (`montybot/browser/tunnel.py`).
///
/// The server asks for a connection by host name and port; this Mac resolves the name itself, refuses unless every
/// address is public (`PublicAddress`) and the port is a web port, and connects to the address it checked, so a
/// name can't change its answer in between. Bytes then flow both ways until either side closes.
///
/// It reconnects by itself, backing off, until stopped. Another Mac of the same user taking over stops it, and so
/// does a server that refuses it (one without the tunnel).
public actor MacTunnel {
    public struct Status: Equatable, Sendable {
        public var connected = false
        /// Open connections: the server's browser is using this Mac right now.
        public var browsing = 0
        /// Another Mac of this user took the tunnel over.
        public var replaced = false

        public init() {}
    }

    private struct Stream {
        let connection: NWConnection
        var opened = false
    }

    private let request: URLRequest
    private let session: URLSession
    private let onStatus: @Sendable (Status) -> Void
    private let queue = DispatchQueue(label: "monty.tunnel")
    private var task: URLSessionWebSocketTask?
    private var running: Task<Void, Never>?
    /// Asked for, and being resolved: no connection yet.
    private var resolving: Set<UInt32> = []
    private var streams: [UInt32: Stream] = [:]
    private var status = Status() {
        didSet { if status != oldValue { onStatus(status) } }
    }

    /// `onStatus` is told of every change of `Status`, on no particular thread.
    public init(request: URLRequest, session: URLSession, onStatus: @escaping @Sendable (Status) -> Void) {
        self.request = request
        self.session = session
        self.onStatus = onStatus
    }

    public func start() {
        guard running == nil else { return }
        running = Task { await run() }
    }

    public func stop() {
        running?.cancel()
        running = nil
        task?.cancel(with: .normalClosure, reason: nil)
        task = nil
        closeAll()
        status = Status()
    }

    private func run() async {
        var delay: Duration = .seconds(1)
        while !Task.isCancelled {
            let task = session.webSocketTask(with: request)
            task.maximumMessageSize = 1024 * 1024
            self.task = task
            task.resume()
            if await answers(task) {
                delay = .seconds(1)
                status.connected = true
                await receive(on: task)
            }
            closeAll()
            let replaced = task.closeCode.rawValue == TunnelWire.replaced
            // Refused before it opened: this server doesn't offer the tunnel (or the session ended, and signing in
            // again starts a new one). Asking again won't change that.
            let refused = (task.response as? HTTPURLResponse)?.statusCode == 403
            status = Status()
            status.replaced = replaced
            if replaced || refused || Task.isCancelled { break }
            try? await Task.sleep(for: delay)
            delay = min(delay * 2, .seconds(60))
        }
    }

    /// Whether the WebSocket opened: a ping is answered only once it has.
    private func answers(_ task: URLSessionWebSocketTask) async -> Bool {
        await withCheckedContinuation { continuation in
            task.sendPing { error in continuation.resume(returning: error == nil) }
        }
    }

    private func receive(on task: URLSessionWebSocketTask) async {
        while !Task.isCancelled, let message = try? await task.receive() {
            guard case .data(let data) = message, let message = TunnelWire.parse(data) else { continue }
            switch message.kind {
            case TunnelWire.open:
                guard let target = TunnelWire.target(message.payload) else { continue }
                resolving.insert(message.stream)
                Task { await open(message.stream, target) }
            case TunnelWire.data:
                streams[message.stream]?.connection.send(content: message.payload, completion: .idempotent)
            case TunnelWire.close:
                drop(message.stream, tellServer: false)
            default:
                continue
            }
        }
    }

    private func open(_ stream: UInt32, _ target: TunnelWire.Target) async {
        guard TunnelWire.webPorts.contains(target.port), let port = NWEndpoint.Port(rawValue: target.port) else {
            return await refuse(stream, TunnelWire.notAllowed)
        }
        let addresses = await Task.detached { PublicAddress.resolve(target.host) }.value
        guard resolving.contains(stream) else { return }  // the server gave up meanwhile
        guard let address = addresses.first else { return await refuse(stream, TunnelWire.unreachable) }
        // Every address public, not just the first: a name that also points inward is up to no good.
        guard addresses.allSatisfy(PublicAddress.allows), let host = Self.host(address) else {
            return await refuse(stream, TunnelWire.notAllowed)
        }
        resolving.remove(stream)
        let tcp = NWProtocolTCP.Options()
        tcp.connectionTimeout = 20
        let connection = NWConnection(host: host, port: port, using: NWParameters(tls: nil, tcp: tcp))
        streams[stream] = Stream(connection: connection)
        status.browsing = streams.count
        connection.stateUpdateHandler = { [weak self] state in
            Task { await self?.changed(stream, connection, state) }
        }
        connection.start(queue: queue)
    }

    private func changed(_ stream: UInt32, _ connection: NWConnection, _ state: NWConnection.State) async {
        guard streams[stream]?.connection === connection else { return }
        switch state {
        case .ready:
            guard streams[stream]?.opened == false else { return }
            streams[stream]?.opened = true
            await send(TunnelWire.opened, stream, Data([TunnelWire.succeeded]))
            read(stream, connection)
        case .failed(let error), .waiting(let error):  // waiting would retry later: too late for the browser
            if streams[stream]?.opened == false {
                let refused = if case .posix(.ECONNREFUSED) = error { true } else { false }
                drop(stream, tellServer: false)
                await send(TunnelWire.opened, stream, Data([refused ? TunnelWire.refused : TunnelWire.unreachable]))
            } else {
                drop(stream, tellServer: true)
            }
        default:
            break
        }
    }

    /// What the site sends, to the server, one read at a time: the next read waits until this one is sent.
    private nonisolated func read(_ stream: UInt32, _ connection: NWConnection) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 64 * 1024) { [weak self] data, _, done, error in
            Task { await self?.received(stream, connection, data, ended: done || error != nil) }
        }
    }

    private func received(_ stream: UInt32, _ connection: NWConnection, _ data: Data?, ended: Bool) async {
        guard streams[stream]?.connection === connection else { return }
        if let data, !data.isEmpty { await send(TunnelWire.data, stream, data) }
        if ended { drop(stream, tellServer: true) } else { read(stream, connection) }
    }

    private func refuse(_ stream: UInt32, _ status: UInt8) async {
        resolving.remove(stream)
        await send(TunnelWire.opened, stream, Data([status]))
    }

    private func drop(_ stream: UInt32, tellServer: Bool) {
        resolving.remove(stream)
        guard let removed = streams.removeValue(forKey: stream) else { return }
        removed.connection.cancel()
        status.browsing = streams.count
        if tellServer { Task { await send(TunnelWire.close, stream) } }
    }

    private func closeAll() {
        for stream in Array(streams.keys) { drop(stream, tellServer: false) }
        resolving = []
    }

    private func send(_ kind: UInt8, _ stream: UInt32, _ payload: Data = Data()) async {
        try? await task?.send(.data(TunnelWire.message(kind, stream, payload)))
    }

    private static func host(_ address: [UInt8]) -> NWEndpoint.Host? {
        switch address.count {
        case 4: IPv4Address(Data(address)).map { .ipv4($0) }
        case 16: IPv6Address(Data(address)).map { .ipv6($0) }
        default: nil
        }
    }
}
