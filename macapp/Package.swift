// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "MarketingStudio",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "MarketingStudio",
            path: "Sources/MarketingStudio"
        )
    ]
)
