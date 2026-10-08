// swift-tools-version: 6.0
// Sammy for Mac: a native client of the Sammy server. `scripts/build-app.sh` makes Sammy.app.
import PackageDescription

let package = Package(
    name: "Sammy",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "Sammy", targets: ["Sammy"]),
    ],
    dependencies: [
        // Telemetry (Telemetry.swift): the API and SDK, and OTLP over HTTP to the server, which forwards to Logfire.
        .package(url: "https://github.com/open-telemetry/opentelemetry-swift-core.git", from: "2.6.0"),
        .package(url: "https://github.com/open-telemetry/opentelemetry-swift.git", from: "2.6.0"),
    ],
    targets: [
        // Everything but the views: the API, the run's live updates, the live view's wire, and the app's state.
        .target(name: "SammyKit", dependencies: [
            .product(name: "OpenTelemetryApi", package: "opentelemetry-swift-core"),
            .product(name: "OpenTelemetrySdk", package: "opentelemetry-swift-core"),
            .product(name: "OpenTelemetryProtocolExporterHTTP", package: "opentelemetry-swift"),
        ]),
        .executableTarget(name: "Sammy", dependencies: ["SammyKit"]),
        .testTarget(name: "SammyKitTests", dependencies: [
            "SammyKit", .product(name: "OpenTelemetrySdk", package: "opentelemetry-swift-core"),
        ]),
    ]
)
