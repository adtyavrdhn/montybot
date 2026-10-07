// swift-tools-version: 6.0
// Monty for Mac: a native client of the montybot server. `scripts/build-app.sh` makes Monty.app.
import PackageDescription

let package = Package(
    name: "Monty",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "Monty", targets: ["Monty"]),
    ],
    targets: [
        // Everything but the views: the API, the run's live updates, the live view's wire, and the app's state.
        .target(name: "MontyKit"),
        .executableTarget(name: "Monty", dependencies: ["MontyKit"]),
        .testTarget(name: "MontyKitTests", dependencies: ["MontyKit"]),
    ]
)
