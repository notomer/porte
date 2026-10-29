// swift-tools-version: 6.0
import PackageDescription

let swiftSettings: [SwiftSetting] = [
    // The app is built with the Swift 6 compiler, but the package stays in Swift
    // 5 language mode: the UI is main-actor bound and strict concurrency adds
    // annotations without changing behaviour here.
    .swiftLanguageMode(.v5)
]

let package = Package(
    name: "PorteMac",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "Porte", targets: ["PorteApp"]),
        .executable(name: "porte-ls", targets: ["PorteLS"]),
        .executable(name: "porte-check", targets: ["PorteCheck"])
    ],
    targets: [
        .target(name: "PorteKit", swiftSettings: swiftSettings),
        .executableTarget(name: "PorteApp", dependencies: ["PorteKit"],
                          swiftSettings: swiftSettings),
        .executableTarget(name: "PorteLS", dependencies: ["PorteKit"],
                          swiftSettings: swiftSettings),
        .executableTarget(name: "PorteCheck", dependencies: ["PorteKit"],
                          swiftSettings: swiftSettings)
    ]
)
