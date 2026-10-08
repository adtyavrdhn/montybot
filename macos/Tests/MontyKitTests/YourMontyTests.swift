import Foundation
import Testing
@testable import MontyKit

/// "Monty" is the product; the user's own Monty goes by the name they gave their squirrel.
@MainActor
struct YourMontyTests {
    let app = AppModel(
        serverURL: URL(string: "https://monty.example.test")!,
        cookies: HTTPCookieStorage.sharedCookieStorage(forGroupContainerIdentifier: "monty-test-your-monty"),
        defaults: UserDefaults(suiteName: "monty-test-your-monty")!
    )

    @Test func itIsMontyUntilTheUserNamesIt() {
        #expect(app.montyName == "Monty")
        app.nameSquirrel("  Sammy ")
        #expect(app.montyName == "Sammy")
        #expect(AppModel.headline(for: .approval, from: app.montyName) == "Sammy needs your OK")
        app.nameSquirrel("")
        #expect(app.montyName == "Monty")
        #expect(AppModel.headline(for: .question, from: app.montyName) == "Monty has a question")
    }
}
