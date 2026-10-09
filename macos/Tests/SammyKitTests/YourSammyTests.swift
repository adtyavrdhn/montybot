import Foundation
import Testing
@testable import SammyKit

/// "Sammy" is the product; the user's own Sammy goes by the name they gave their squirrel.
@MainActor
struct YourSammyTests {
    let app = AppModel(
        serverURL: URL(string: "https://sammy.example.test")!,
        cookies: HTTPCookieStorage.sharedCookieStorage(forGroupContainerIdentifier: "sammy-test-your-sammy"),
        defaults: UserDefaults(suiteName: "sammy-test-your-sammy")!
    )

    @Test func itIsSammyUntilTheUserNamesIt() {
        #expect(app.sammyName == "Sammy")
        app.nameSquirrel("  Sammy ")
        #expect(app.sammyName == "Sammy")
        #expect(AppModel.headline(for: .approval, from: app.sammyName) == "Sammy needs your OK")
        app.nameSquirrel("")
        #expect(app.sammyName == "Sammy")
        #expect(AppModel.headline(for: .question, from: app.sammyName) == "Sammy has a question")
    }
}
