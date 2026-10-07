// swift-tools-version: 6.0
// Monty for Mac: a native client of the montybot server. `scripts/build-app.sh` makes Monty.app.
import PackageDescription

let package = Package(
    name: "Monty",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "Monty", targets: ["Monty"]),
    ],
    dependencies: [
        // Telemetry (Telemetry.swift): the API and SDK, and OTLP over HTTP to the server, which forwards to Logfire.
        .package(url: "https://github.com/open-telemetry/opentelemetry-swift-core.git", from: "2.6.0"),
        .package(url: "https://github.com/open-telemetry/opentelemetry-swift.git", from: "2.6.0"),
    ],
    targets: [
        // Everything but the views: the API, the run's live updates, the live view's wire, and the app's state.
        .target(name: "MontyKit", dependencies: [
            .product(name: "OpenTelemetryApi", package: "opentelemetry-swift-core"),
            .product(name: "OpenTelemetrySdk", package: "opentelemetry-swift-core"),
            .product(name: "OpenTelemetryProtocolExporterHTTP", package: "opentelemetry-swift"),
        ]),
        .executableTarget(name: "Monty", dependencies: ["MontyKit"]),
        .testTarget(name: "MontyKitTests", dependencies: [
            "MontyKit", .product(name: "OpenTelemetrySdk", package: "opentelemetry-swift-core"),
        ]),
    ]
)
