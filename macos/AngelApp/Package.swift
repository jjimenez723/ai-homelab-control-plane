// swift-tools-version: 6.3
import PackageDescription

let package = Package(
    name: "AngelApp",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "AngelApp", targets: ["AngelApp"]),
    ],
    targets: [
        .executableTarget(
            name: "AngelApp",
            path: "Sources/AngelApp",
            swiftSettings: [
                .defaultIsolation(MainActor.self),
            ]
        ),
        .testTarget(
            name: "AngelAppTests",
            dependencies: ["AngelApp"],
            path: "Tests/AngelAppTests"
        ),
    ]
)
