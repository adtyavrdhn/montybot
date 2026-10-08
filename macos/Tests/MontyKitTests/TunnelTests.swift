import Foundation
import Testing
@testable import MontyKit

// The Mac tunnel's wire, checked against what `montybot/browser/tunnel.py` sends and accepts, and which addresses
// the tunnel may reach.

@Suite struct TunnelWireTests {
    @Test func messagesAreKindStreamPayload() {
        // tunnel.message(OPENED, 258, b'\x00') in Python
        #expect(TunnelWire.message(TunnelWire.opened, 258, Data([0])) == Data([2, 0, 0, 1, 2, 0]))
        #expect(TunnelWire.parse(Data([3, 0, 0, 1, 2]) + Data("hi".utf8))
            == TunnelWire.Message(kind: TunnelWire.data, stream: 258, payload: Data("hi".utf8)))
        #expect(TunnelWire.parse(Data([4, 0, 0, 0])) == nil)
    }

    @Test func openNamesThePortThenTheHost() {
        #expect(TunnelWire.target(Data([1, 187]) + Data("shop.example".utf8)) == TunnelWire.Target(host: "shop.example", port: 443))
        #expect(TunnelWire.target(Data([0, 80])) == nil)
    }
}

@Suite struct PublicAddressTests {
    @Test func publicAddressesOnly() {
        #expect(PublicAddress.allows([93, 184, 216, 34]))
        #expect(PublicAddress.allows([8, 8, 8, 8]))
        for refused: [UInt8] in [
            [127, 0, 0, 1], [10, 1, 2, 3], [172, 16, 0, 1], [172, 31, 255, 255], [192, 168, 1, 1], [169, 254, 169, 254],
            [100, 64, 0, 1], [0, 0, 0, 0], [224, 0, 0, 1], [255, 255, 255, 255], [198, 18, 0, 1], [192, 0, 2, 1],
        ] {
            #expect(!PublicAddress.allows(refused), "\(refused)")
        }
        #expect(PublicAddress.allows([172, 32, 0, 1]))
    }

    @Test func ipv6GlobalUnicastOnly() {
        let google: [UInt8] = [0x26, 0x07, 0xf8, 0xb0] + Array(repeating: 0, count: 11) + [0x01]
        #expect(PublicAddress.allows(google))
        let loopback = Array(repeating: UInt8(0), count: 15) + [1]
        let linkLocal: [UInt8] = [0xfe, 0x80] + Array(repeating: 0, count: 13) + [1]
        let uniqueLocal: [UInt8] = [0xfd, 0x00] + Array(repeating: 0, count: 13) + [1]
        let sixToFour: [UInt8] = [0x20, 0x02, 192, 168] + Array(repeating: 0, count: 12)
        let documentation: [UInt8] = [0x20, 0x01, 0x0d, 0xb8] + Array(repeating: 0, count: 12)
        for refused in [loopback, linkLocal, uniqueLocal, sixToFour, documentation] {
            #expect(!PublicAddress.allows(refused), "\(refused)")
        }
    }

    @Test func mappedIPv4IsJudgedAsIPv4() {
        let mapped = { (v4: [UInt8]) in Array(repeating: UInt8(0), count: 10) + [0xff, 0xff] + v4 }
        #expect(!PublicAddress.allows(mapped([127, 0, 0, 1])))
        #expect(PublicAddress.allows(mapped([8, 8, 8, 8])))
    }

    @Test func resolvesLiteralsWithoutTheNetwork() {
        #expect(PublicAddress.resolve("127.0.0.1") == [[127, 0, 0, 1]])
        #expect(PublicAddress.resolve("nonexistent.invalid").isEmpty)
    }
}
