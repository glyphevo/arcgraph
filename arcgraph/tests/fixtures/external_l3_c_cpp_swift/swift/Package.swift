// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "ArcGraphDemo",
    products: [
        .library(name: "ArcGraphDemo", targets: ["ArcGraphDemo"])
    ],
    targets: [
        .target(name: "ArcGraphDemo")
    ]
)
