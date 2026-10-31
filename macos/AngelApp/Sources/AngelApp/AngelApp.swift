import AppKit
import Foundation
import ImageIO
import Observation
import SwiftUI
import UniformTypeIdentifiers

@main
struct AngelApp: App {
    @State private var model = AngelModel()

    var body: some Scene {
        WindowGroup("Angel") {
            ContentView(model: model)
        }
        .defaultSize(width: 760, height: 620)

        Window("Analysis Monitor", id: "analysis-monitor") {
            AngelAnalysisMonitorView(model: model)
        }
        .defaultSize(width: 760, height: 640)
    }
}

enum AngelTarget: String, CaseIterable, Identifiable {
    case all
    case desktop
    case documents
    case downloads
    case sd

    var id: String { rawValue }

    var title: String {
        switch self {
        case .all: "All sources"
        case .desktop: "Desktop"
        case .documents: "Documents"
        case .downloads: "Downloads"
        case .sd: "SD cards"
        }
    }
}

struct CommandResult: Sendable {
    let status: Int32
    let output: String
}

struct ActivePreflightAnalysis: Sendable {
    let processID: Int32
    let target: String
    let catalogPath: String
    let pathsURL: URL
    let progressURL: URL
}

enum CommandRunnerError: LocalizedError, Equatable {
    case timedOut(seconds: Int)

    var errorDescription: String? {
        switch self {
        case let .timedOut(seconds):
            "The operation timed out after \(seconds) seconds. The helper was stopped; no partial result was applied."
        }
    }
}

struct AngelPlanSummary: Codable, Equatable, Sendable {
    let fileCount: Int
    let totalBytes: Int64
    let warningCount: Int
}

struct AngelPlanAnalysis: Codable, Equatable, Sendable {
    let status: String
    let message: String
}

struct AngelPersonDetection: Codable, Equatable, Sendable {
    let personProfileID: String?
    let confidence: Double?
    let evidence: String
}

struct AngelProfileMatch: Codable, Equatable, Sendable {
    let profileID: String
    let displayName: String
    let confidence: Double?
    let evidence: String
}

struct AngelAnimalDetection: Codable, Equatable, Sendable {
    let animalType: String
    let count: Int
    let confidence: Double?
    let evidence: String
}

struct AngelFilenameProposal: Codable, Equatable, Sendable {
    let subject: String?
    let variant: String?
    let confidence: Double?
}

struct AngelFileAnalysis: Codable, Equatable, Sendable {
    let status: String
    let summary: String?
    let people: [AngelPersonDetection]?
    let profileMatches: [AngelProfileMatch]?
    let animals: [AngelAnimalDetection]?
    let tags: [String]?
    let filenameProposal: AngelFilenameProposal?
    let proposedFilename: String?
    let destinationRelativePath: String?
    let collisionResolution: String?
    let error: String?

    var isReadyForReview: Bool {
        status == "ready_for_review" && error == nil
    }

    var peopleText: String? {
        guard let profileMatches, !profileMatches.isEmpty else { return nil }
        return profileMatches.map(\.displayName).joined(separator: ", ")
    }

    var animalsText: String? {
        guard let animals, !animals.isEmpty else { return nil }
        return animals.map { "\($0.count) \($0.animalType)" }.joined(separator: ", ")
    }
}

struct AngelPlanFile: Codable, Equatable, Identifiable, Sendable {
    let sourcePath: String
    let relativePath: String
    let sourceClass: String
    let sourceVolume: String
    let currentFilename: String
    let proposedFilename: String
    let landingDestination: String
    let status: String
    let warning: String?
    let size: Int64
    let mtimeNs: Int64
    let analysis: AngelFileAnalysis?

    var id: String { sourcePath }
}

struct AngelApprovedPlanFile: Codable, Sendable {
    let sourcePath: String
    let destinationRelativePath: String
}

struct AngelApprovedPlan: Codable, Sendable {
    let fingerprint: String
    let files: [AngelApprovedPlanFile]
}

struct AngelPreviewPlan: Codable, Equatable, Sendable {
    let schemaVersion: Int
    let fingerprint: String
    let destinationRoot: String
    let catalogPath: String?
    let summary: AngelPlanSummary
    let analysis: AngelPlanAnalysis
    let warnings: [String]
    let files: [AngelPlanFile]

    var analysisMessage: String { analysis.message }

    static func decode(_ output: String) throws -> AngelPreviewPlan {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(AngelPreviewPlan.self, from: Data(output.utf8))
    }

    var summaryText: String {
        let fileLabel = summary.fileCount == 1 ? "file" : "files"
        let byteLabel = ByteCountFormatter.string(fromByteCount: summary.totalBytes, countStyle: .file)
        return "\(summary.fileCount) \(fileLabel) • \(byteLabel) • \(summary.warningCount) warning(s)"
    }
}

struct AngelAnalysisProgress: Codable, Sendable {
    let event: String
    let sourcePath: String
    let completed: Int
    let total: Int
    let file: AngelPlanFile?

    static func decode(_ line: String) throws -> AngelAnalysisProgress {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(AngelAnalysisProgress.self, from: Data(line.utf8))
    }
}

struct AngelMountedVolume: Identifiable, Equatable, Sendable {
    let id: String
    let name: String
    let mountPath: String
    let writable: Bool

    var displayPath: String {
        mountPath
    }
}

enum AngelOperationState: Equatable {
    case ready
    case running(String)
    case succeeded(String)
    case failed(String)

    var isRunning: Bool {
        if case .running = self {
            return true
        }
        return false
    }

    var statusText: String {
        switch self {
        case .ready:
            "Ready."
        case let .running(message), let .succeeded(message), let .failed(message):
            message
        }
    }

    var symbolName: String {
        switch self {
        case .ready:
            "circle"
        case .running:
            "arrow.triangle.2.circlepath"
        case .succeeded:
            "checkmark.circle.fill"
        case .failed:
            "exclamationmark.circle.fill"
        }
    }
}

enum AngelOperation {
    case preview
    case preflightAnalysis
    case importFiles
    case analysis

    var displayName: String {
        switch self {
        case .preview:
            "Preview"
        case .preflightAnalysis:
            "Pre-ingest analysis"
        case .importFiles:
            "Import"
        case .analysis:
            "Asset analysis"
        }
    }

    var isPlanOperation: Bool {
        switch self {
        case .preview, .preflightAnalysis:
            true
        case .importFiles, .analysis:
            false
        }
    }
}

enum AngelProjectRootError: LocalizedError, Equatable {
    case notDirectory(URL)
    case missingOrganizerScript(URL)

    var errorDescription: String? {
        switch self {
        case let .notDirectory(url):
            "That location is not a folder: \(url.path)"
        case let .missingOrganizerScript(url):
            "That folder does not contain an executable scripts/organize: \(url.path)"
        }
    }

    var recoverySuggestion: String? {
        switch self {
        case .notDirectory:
            "Choose the Angel project folder."
        case .missingOrganizerScript:
            "Choose the folder containing scripts/organize, such as the Angel project workspace root."
        }
    }
}

struct AngelProjectRootResolver {
    static let bookmarkKey = "Angel.projectRootBookmark"

    let defaults: UserDefaults
    let homeDirectory: URL
    let bundleURL: URL
    let fileManager: FileManager
    private let injectedKnownRoots: [URL]?

    init(
        defaults: UserDefaults = .standard,
        homeDirectory: URL = FileManager.default.homeDirectoryForCurrentUser,
        bundleURL: URL = Bundle.main.bundleURL,
        fileManager: FileManager = .default,
        knownRoots: [URL]? = nil
    ) {
        self.defaults = defaults
        self.homeDirectory = homeDirectory
        self.bundleURL = bundleURL
        self.fileManager = fileManager
        self.injectedKnownRoots = knownRoots
    }

    var prompt: String {
        "Choose the Angel project folder containing scripts/organize."
    }

    func validate(_ url: URL) throws -> URL {
        let root = url.resolvingSymlinksInPath().standardizedFileURL
        var isDirectory = ObjCBool(false)
        guard fileManager.fileExists(atPath: root.path, isDirectory: &isDirectory), isDirectory.boolValue else {
            throw AngelProjectRootError.notDirectory(root)
        }

        let organizer = root.appendingPathComponent("scripts/organize")
        guard fileManager.isExecutableFile(atPath: organizer.path) else {
            throw AngelProjectRootError.missingOrganizerScript(root)
        }
        return root
    }

    func resolve() -> URL? {
        if let savedRoot = resolveSavedBookmark() {
            do {
                return try validate(savedRoot)
            } catch {
                defaults.removeObject(forKey: Self.bookmarkKey)
            }
        }

        for candidate in knownRoots {
            if let validRoot = try? validate(candidate) {
                return validRoot
            }
        }
        return nil
    }

    func save(_ root: URL) throws {
        let validRoot = try validate(root)
        let bookmark = try validRoot.bookmarkData(
            options: [.withSecurityScope],
            includingResourceValuesForKeys: nil,
            relativeTo: nil
        )
        defaults.set(bookmark, forKey: Self.bookmarkKey)
    }

    private var knownRoots: [URL] {
        if let injectedKnownRoots {
            return injectedKnownRoots
        }

        let workspaceCandidates = [
            homeDirectory.appendingPathComponent("Documents/ai-homelab-control-plane"),
            homeDirectory.appendingPathComponent("Projects/ai-homelab-control-plane")
        ]

        var bundleCandidates: [URL] = []
        var candidate = bundleURL
        for _ in 0..<4 {
            candidate = candidate.deletingLastPathComponent()
            bundleCandidates.append(candidate)
        }

        return workspaceCandidates + bundleCandidates
    }

    private func resolveSavedBookmark() -> URL? {
        guard let data = defaults.data(forKey: Self.bookmarkKey) else {
            return nil
        }

        var isStale = false
        guard let url = try? URL(
            resolvingBookmarkData: data,
            options: [.withSecurityScope],
            relativeTo: nil,
            bookmarkDataIsStale: &isStale
        ) else {
            defaults.removeObject(forKey: Self.bookmarkKey)
            return nil
        }

        if isStale {
            try? save(url)
        }
        return url
    }
}

nonisolated enum CommandRunner {
    static func execute(
        executable: URL,
        arguments: [String],
        timeout: TimeInterval? = nil
    ) throws -> CommandResult {
        let process = Process()
        let pipe = Pipe()
        process.executableURL = executable
        process.arguments = arguments
        process.standardOutput = pipe
        process.standardError = pipe
        try process.run()

        let timeoutLock = NSLock()
        var didTimeOut = false
        let watchdog: DispatchWorkItem?
        if let timeout {
            let workItem = DispatchWorkItem {
                guard process.isRunning else { return }
                timeoutLock.lock()
                didTimeOut = true
                timeoutLock.unlock()
                process.terminate()
            }
            watchdog = workItem
            DispatchQueue.global(qos: .userInitiated).asyncAfter(
                deadline: .now() + timeout,
                execute: workItem
            )
        } else {
            watchdog = nil
        }

        // Drain the pipe before waiting for termination. Waiting first can
        // deadlock when a large preview fills the pipe's finite buffer.
        let outputData = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        watchdog?.cancel()

        timeoutLock.lock()
        let timedOut = didTimeOut
        timeoutLock.unlock()
        if timedOut, let timeout {
            throw CommandRunnerError.timedOut(seconds: max(1, Int(timeout.rounded())))
        }

        let output = String(decoding: outputData, as: UTF8.self)
        return CommandResult(status: process.terminationStatus, output: output)
    }

    static func execute(
        script: URL,
        arguments: [String],
        timeout: TimeInterval? = nil
    ) throws -> CommandResult {
        try execute(executable: script, arguments: arguments, timeout: timeout)
    }
}

@Observable
@MainActor
final class AngelModel {
    private let projectResolver: AngelProjectRootResolver
    private var projectAccessScopeIsActive = false

    var projectURL: URL?
    var catalogURL: URL?
    var assetURL: URL?
    var appleTeamID = ""
    var appStoreConnectIssuerID = ""
    var appStoreConnectKeyID = ""
    var privateKeyURL: URL?
    var appleCredentialsStatus = AngelKeychain.hasCredentials() ? "Credentials stored in macOS Keychain." : "No credentials stored."
    var selectedTarget: AngelTarget = .sd {
        didSet {
            if oldValue != selectedTarget {
                previewPlan = nil
                lastPreviewAt = nil
            }
        }
    }
    var output = "Select an Angel project folder to begin."
    var previewState: AngelOperationState = .ready
    var preflightAnalysisState: AngelOperationState = .ready
    var importState: AngelOperationState = .ready
    var analysisState: AngelOperationState = .ready
    var mountedRemovableVolumes: [AngelMountedVolume] = []
    var registeredSDVolumeIDs: Set<String> = []
    var storageStatus = "Checking for connected SD cards…"
    var isRefreshingStorage = false
    var showImportConfirmation = false
    var lastPreviewAt: Date?
    var previewPlan: AngelPreviewPlan?
    var reviewFilter = ""
    var analysisSelection: Set<String> = []
    var approvedFilePaths: Set<String> = []
    var analysisCurrentSourcePath: String?
    var analysisLatestSourcePath: String?
    var analysisHistorySourcePaths: [String] = []
    private var temporaryPlanFiles: [URL] = []
    private var progressMonitor: Task<Void, Never>?
    private var preflightQueue: [String] = []
    private var preflightSelectedPaths: [String] = []
    private var preflightCatalogPath: String?
    private var preflightAnalysisTotal = 0
    private var preflightBatchCompletedBase = 0

    // Ollama analysis is intentionally serial on the local model. Keeping a
    // batch bounded prevents one long-running subprocess from losing all of
    // its completed work at the old fixed 15-minute deadline.
    private let preflightBatchSize = 20

    init(projectResolver: AngelProjectRootResolver = AngelProjectRootResolver()) {
        self.projectResolver = projectResolver

        if let resolvedRoot = projectResolver.resolve() {
            projectURL = resolvedRoot
            projectAccessScopeIsActive = resolvedRoot.startAccessingSecurityScopedResource()
            output = "Project automatically selected: \(resolvedRoot.path)\nChoose an operation."
        } else {
            let message = projectResolver.prompt
            previewState = .failed(message)
            output = message
        }

        Task { [weak self] in
            self?.refreshStorage()
            self?.reconnectToActivePreflightAnalysis()
        }
    }

    var isRunning: Bool {
        previewState.isRunning || preflightAnalysisState.isRunning || importState.isRunning || analysisState.isRunning
    }

    var canImport: Bool {
        projectURL != nil &&
            previewPlan != nil &&
            !approvedFilePaths.isEmpty &&
            !isRunning &&
            (selectedTarget != .sd || registeredSDVolumeIDs.isEmpty == false)
    }

    var previewStatusText: String {
        guard let lastPreviewAt else { return "Preview never changes files." }
        return "Last refreshed \(lastPreviewAt.formatted(date: .omitted, time: .standard))"
    }

    var filteredPreviewFiles: [AngelPlanFile] {
        guard let previewPlan else { return [] }
        let needle = reviewFilter.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !needle.isEmpty else { return previewPlan.files }
        return previewPlan.files.filter { file in
            [file.currentFilename, file.proposedFilename, file.sourcePath, file.landingDestination]
                .contains { $0.localizedCaseInsensitiveContains(needle) }
        }
    }

    var reviewableFileCount: Int {
        previewPlan?.files.filter { $0.analysis?.isReadyForReview == true }.count ?? 0
    }

    var approvedReviewableFileCount: Int {
        previewPlan?.files.filter {
            $0.analysis?.isReadyForReview == true && approvedFilePaths.contains($0.sourcePath)
        }.count ?? 0
    }

    var currentAnalysisFile: AngelPlanFile? {
        guard let sourcePath = analysisCurrentSourcePath else { return nil }
        return previewPlan?.files.first { $0.sourcePath == sourcePath }
    }

    var latestAnalysisFile: AngelPlanFile? {
        guard let sourcePath = analysisLatestSourcePath else { return nil }
        return previewPlan?.files.first { $0.sourcePath == sourcePath }
    }

    var analysisHistoryFiles: [AngelPlanFile] {
        analysisHistorySourcePaths.compactMap { sourcePath in
            previewPlan?.files.first { $0.sourcePath == sourcePath }
        }
    }

    func chooseProject() {
        let panel = NSOpenPanel()
        panel.title = "Choose the Angel project folder"
        panel.message = "Choose the folder containing scripts/organize."
        panel.prompt = "Choose Project Folder"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK, let url = panel.url {
            do {
                let validRoot = try projectResolver.validate(url)
                try projectResolver.save(validRoot)
                setProjectRoot(validRoot)
                previewState = .ready
                output = "Project selected: \(validRoot.path)\nChoose an operation."
                refreshStorage()
            } catch let error as AngelProjectRootError {
                let message = [error.localizedDescription, error.recoverySuggestion]
                    .compactMap { $0 }
                    .joined(separator: "\n")
                previewState = .failed(message)
                output = message
            } catch {
                let message = "Could not save the selected project folder: \(error.localizedDescription)"
                previewState = .failed(message)
                output = message
            }
        }
    }

    func chooseCatalog() {
        let panel = NSOpenPanel()
        panel.title = "Choose Angel's catalog.sqlite"
        panel.message = "Choose the catalog on the Angel volume."
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.allowedContentTypes = [.data]
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK {
            catalogURL = panel.url
            output = "Catalog selected: \(panel.url?.path ?? "")"
        }
    }

    func chooseAsset() {
        let panel = NSOpenPanel()
        panel.title = "Choose a verified asset to analyze"
        panel.message = "Choose an asset already present in the catalog."
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK {
            assetURL = panel.url
            output = "Asset selected: \(panel.url?.path ?? "")"
        }
    }

    func preview() {
        previewPlan = nil
        reviewFilter = ""
        analysisSelection = []
        approvedFilePaths = []
        analysisCurrentSourcePath = nil
        analysisLatestSourcePath = nil
        analysisHistorySourcePaths = []
        preflightQueue = []
        preflightSelectedPaths = []
        preflightCatalogPath = nil
        preflightAnalysisTotal = 0
        preflightBatchCompletedBase = 0
        lastPreviewAt = nil
        run(
            operation: .preview,
            runningMessage: "Previewing \(selectedTarget.title.lowercased())…",
            scriptName: "scripts/organize",
            arguments: [selectedTarget.rawValue, "--dry-run", "--json"],
            timeout: 15 * 60
        )
    }

    func toggleAnalysisSelection(for sourcePath: String, selected: Bool) {
        if selected {
            analysisSelection.insert(sourcePath)
        } else {
            analysisSelection.remove(sourcePath)
        }
    }

    func toggleApproval(for file: AngelPlanFile, approved: Bool) {
        guard file.analysis?.isReadyForReview == true else { return }
        if approved {
            approvedFilePaths.insert(file.sourcePath)
        } else {
            approvedFilePaths.remove(file.sourcePath)
        }
    }

    func selectAllForAnalysis() {
        analysisSelection = Set(filteredPreviewFiles.map(\.sourcePath))
    }

    func selectAllReviewedFilesForMove() {
        approveAndRenameAll()
    }

    func approveAndRenameAll() {
        guard let previewPlan else { return }
        let reviewableFiles = previewPlan.files.filter { $0.analysis?.isReadyForReview == true }
        guard !reviewableFiles.isEmpty else {
            output = "Analyze files first. There are no reviewed proposals to approve."
            return
        }
        approvedFilePaths = Set(reviewableFiles.map(\.sourcePath))
        let collisionCount = reviewableFiles.filter { $0.analysis?.collisionResolution != nil }.count
        let collisionNote = collisionCount == 0
            ? ""
            : " \(collisionCount) duplicate model name(s) received deterministic suffixes."
        output = "Approved \(reviewableFiles.count) file(s).\(collisionNote) Click Move & rename approved files to apply them after verification."
    }

    func reconnectToActivePreflightAnalysis() {
        guard !isRunning, let projectURL else { return }

        Task { [weak self] in
            let active = await Task.detached(priority: .utility) {
                Self.discoverActivePreflightAnalysis()
            }.value
            guard let self, let active else { return }

            guard let paths = try? String(contentsOf: active.pathsURL, encoding: .utf8)
                .split(whereSeparator: \.isNewline)
                .map(String.init), !paths.isEmpty
            else { return }

            let script = projectURL.appendingPathComponent("scripts/organize")
            let result = await Task.detached(priority: .utility) {
                try? CommandRunner.execute(
                    script: script,
                    arguments: [active.target, "--dry-run", "--json"],
                    timeout: 15
                )
            }.value
            guard let result, result.status == 0,
                  let plan = try? AngelPreviewPlan.decode(result.output)
            else { return }

            guard AngelTarget(rawValue: active.target) != nil else { return }
            selectedTarget = AngelTarget(rawValue: active.target) ?? selectedTarget
            previewPlan = plan
            lastPreviewAt = Date()
            analysisSelection = Set(paths)
            approvedFilePaths = []
            analysisCurrentSourcePath = paths.first
            analysisLatestSourcePath = nil
            analysisHistorySourcePaths = []
            preflightQueue = []
            preflightSelectedPaths = paths
            preflightCatalogPath = active.catalogPath
            preflightAnalysisTotal = paths.count
            preflightBatchCompletedBase = 0
            preflightAnalysisState = .running("Reconnected to the active analysis worker. Reading its progress…")
            output = renderPreview(plan, message: "Reconnected to the active analysis worker.")
            progressMonitor?.cancel()
            progressMonitor = monitorAnalysisProgress(at: active.progressURL)
        }
    }

    func analyzeSelectedPreviewFiles() {
        guard let previewPlan else {
            output = "Refresh preview first."
            return
        }
        guard !analysisSelection.isEmpty else {
            output = "Select at least one file in the review plan before analyzing."
            return
        }
        guard let catalogPath = previewPlan.catalogPath else {
            output = "The T7 catalog is not available. Connect the T7 and refresh preview first."
            return
        }

        let selectedPaths = analysisSelection.sorted()
        let alreadyAnalyzed = Set(
            previewPlan.files
                .filter { selectedPaths.contains($0.sourcePath) && $0.analysis?.status == "ready_for_review" }
                .map(\.sourcePath)
        )
        let pendingPaths = selectedPaths.filter { !alreadyAnalyzed.contains($0) }
        guard !pendingPaths.isEmpty else {
            output = "All selected files already have analysis proposals. Review them below."
            return
        }

        // A fresh model pass can change the proposed name or folder. Require
        // the owner to review those new proposals again instead of carrying
        // approvals across a changed analysis result. Completed rows are kept
        // when a long run is resumed after a timeout or a transient failure.
        approvedFilePaths = []
        preflightSelectedPaths = selectedPaths
        preflightCatalogPath = catalogPath
        preflightQueue = pendingPaths
        preflightAnalysisTotal = selectedPaths.count
        preflightBatchCompletedBase = alreadyAnalyzed.count
        startNextPreflightBatch()
    }

    private func startNextPreflightBatch() {
        guard !preflightQueue.isEmpty else {
            finishPreflightAnalysis()
            return
        }
        let batch = Array(preflightQueue.prefix(preflightBatchSize))
        preflightQueue.removeFirst(batch.count)
        preflightBatchCompletedBase = preflightAnalysisTotal - preflightQueue.count - batch.count
        guard let catalogPath = preflightCatalogPath else {
            let message = "The T7 catalog is no longer available. Refresh preview and try again."
            setState(.failed(message), for: .preflightAnalysis)
            output = message
            return
        }

        let pathsFile = FileManager.default.temporaryDirectory
            .appendingPathComponent("angel-analysis-\(UUID().uuidString).txt")
        let progressFile = FileManager.default.temporaryDirectory
            .appendingPathComponent("angel-analysis-progress-\(UUID().uuidString).jsonl")
        do {
            try batch.joined(separator: "\n").write(to: pathsFile, atomically: true, encoding: .utf8)
            FileManager.default.createFile(atPath: progressFile.path, contents: Data())
            temporaryPlanFiles.append(pathsFile)
            temporaryPlanFiles.append(progressFile)
        } catch {
            let message = "Could not prepare the analysis selection: \(error.localizedDescription)"
            setState(.failed(message), for: .preflightAnalysis)
            output = message
            return
        }

        run(
            operation: .preflightAnalysis,
            runningMessage: "Analyzing \(preflightBatchCompletedBase + 1)…\(preflightBatchCompletedBase + batch.count) of \(preflightAnalysisTotal) selected file(s) without moving them…",
            scriptName: "scripts/organize",
            arguments: [
                selectedTarget.rawValue,
                "--dry-run",
                "--json",
                "--analyze",
                "--catalog",
                catalogPath,
                "--analysis-paths-file",
                pathsFile.path,
                "--progress-file",
                progressFile.path,
            ],
            // The Python client bounds each individual Ollama request. Give a
            // batch enough time for those per-file limits plus process/setup
            // overhead, while still keeping retries and partial progress
            // bounded. This is intentionally derived from batch size rather
            // than from the total selection (which may contain thousands of
            // files).
            timeout: max(15 * 60, TimeInterval(batch.count * 240 + 120)),
            cleanupURL: pathsFile,
            progressURL: progressFile
        )
    }

    private func advancePreflightAnalysis() {
        guard let plan = previewPlan else { return }
        guard !preflightQueue.isEmpty else {
            finishPreflightAnalysis()
            return
        }

        let processed = preflightAnalysisTotal - preflightQueue.count
        let message = "Analyzed \(processed) of \(preflightAnalysisTotal) selected file(s). Continuing…"
        previewPlan = updatingPreflightPlan(plan, status: "running", message: message)
        setState(.succeeded(message), for: .preflightAnalysis)
        output = renderPreview(previewPlan!, message: message)
        startNextPreflightBatch()
    }

    private func finishPreflightAnalysis() {
        guard let previewPlan else { return }
        let analyzedPaths = Set(
            previewPlan.files
                .filter { preflightSelectedPaths.contains($0.sourcePath) && $0.analysis?.status == "ready_for_review" }
                .map(\.sourcePath)
        )
        let failedCount = preflightSelectedPaths.reduce(into: 0) { count, path in
            guard let file = previewPlan.files.first(where: { $0.sourcePath == path }) else { return }
            if file.analysis?.status == "failed" { count += 1 }
        }

        if analyzedPaths.count + failedCount < preflightSelectedPaths.count {
            let message = "Analyzed \(analyzedPaths.count) of \(preflightAnalysisTotal) selected file(s)."
            self.previewPlan = updatingPreflightPlan(previewPlan, status: "partial_failed", message: message)
            setState(.failed(message), for: .preflightAnalysis)
            output = renderPreview(self.previewPlan!, message: message)
            return
        }

        let message: String
        if failedCount > 0 {
            message = "Analysis completed for \(analyzedPaths.count) file(s); \(failedCount) failed. Failed files cannot be approved."
        } else {
            message = "Analysis completed for \(analyzedPaths.count) file(s). Review each proposal before moving."
        }
        let status = failedCount > 0 ? "partial_failed" : "complete"
        self.previewPlan = updatingPreflightPlan(previewPlan, status: status, message: message)
        analysisSelection = []
        preflightQueue = []
        preflightSelectedPaths = []
        preflightCatalogPath = nil
        setState(failedCount > 0 ? .failed(message) : .succeeded(message), for: .preflightAnalysis)
        output = renderPreview(self.previewPlan!, message: message)
    }

    func requestImport() {
        guard canImport else {
            output = previewPlan == nil
                ? "Refresh preview first so Angel can apply the reviewed source plan."
                : selectedTarget == .sd
                ? "Connect an SD card and choose Use this card before importing."
                : projectResolver.prompt
            return
        }
        showImportConfirmation = true
    }

    func importFiles() {
        guard let previewPlan else {
            requestImport()
            return
        }
        let approvedFiles = previewPlan.files.compactMap { file -> AngelApprovedPlanFile? in
            guard approvedFilePaths.contains(file.sourcePath),
                  let destinationRelativePath = file.analysis?.destinationRelativePath
            else { return nil }
            return AngelApprovedPlanFile(
                sourcePath: file.sourcePath,
                destinationRelativePath: destinationRelativePath
            )
        }
        guard !approvedFiles.isEmpty else {
            output = "Analyze files, then approve at least one proposal before moving anything."
            return
        }
        let approvedPlan = AngelApprovedPlan(fingerprint: previewPlan.fingerprint, files: approvedFiles)
        let approvedURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("angel-approved-\(UUID().uuidString).json")
        do {
            let encoder = JSONEncoder()
            encoder.keyEncodingStrategy = .convertToSnakeCase
            try encoder.encode(approvedPlan).write(to: approvedURL, options: .atomic)
            temporaryPlanFiles.append(approvedURL)
        } catch {
            output = "Could not prepare the approved move plan: \(error.localizedDescription)"
            return
        }
        let arguments = [
            selectedTarget.rawValue,
            "--apply",
            "--plan-fingerprint",
            previewPlan.fingerprint,
            "--approved-plan",
            approvedURL.path,
        ]
        run(
            operation: .importFiles,
            runningMessage: "Moving files from \(selectedTarget.title.lowercased()) to the Angel drive…",
            scriptName: "scripts/organize",
            arguments: arguments,
            cleanupURL: approvedURL
        )
    }

    func refreshStorage() {
        guard !isRefreshingStorage else { return }
        isRefreshingStorage = true
        storageStatus = "Checking for connected SD cards…"

        Task {
            let volumes = await Task.detached(priority: .userInitiated) {
                Self.probeRemovableVolumes()
            }.value

            let t7VolumeID = readConfiguredT7VolumeID()
            let sdVolumes = volumes.filter { $0.id != t7VolumeID }
            mountedRemovableVolumes = sdVolumes
            registeredSDVolumeIDs = readRegisteredSDVolumeIDs()
            isRefreshingStorage = false

            if selectedTarget == .sd && registeredSDVolumeIDs.isEmpty {
                previewPlan = nil
                lastPreviewAt = nil
            }

            if sdVolumes.isEmpty {
                storageStatus = "No removable SD card is mounted. Insert a card, then click Refresh."
            } else if let ready = sdVolumes.first(where: { registeredSDVolumeIDs.contains($0.id) }) {
                storageStatus = "Ready: \(ready.name) is registered for import."
            } else {
                storageStatus = "SD card found. Click Use this card to enable it."
            }
        }
    }

    func registerSDVolume(_ volume: AngelMountedVolume) {
        guard let projectURL else {
            output = projectResolver.prompt
            return
        }

        do {
            try Self.registerSDVolumeID(volume.id, projectRoot: projectURL)
            registeredSDVolumeIDs.insert(volume.id)
            selectedTarget = .sd
            storageStatus = "Ready: \(volume.name) is registered for import."
            output = "SD card ready: \(volume.name)\n\(volume.displayPath)\n\nClick Preview dry run to see the files, then Move files to T7."
            preview()
        } catch {
            output = "Could not prepare this SD card.\n\n\(error.localizedDescription)"
        }
    }

    func analyze() {
        guard let catalogURL, let assetURL else {
            let message = "Choose both a catalog and an asset before analyzing."
            analysisState = .failed(message)
            output = message
            return
        }
        run(
            operation: .analysis,
            runningMessage: "Analyzing selected asset…",
            scriptName: "scripts/analyze-media",
            arguments: ["analyze", assetURL.path, "--catalog", catalogURL.path],
            timeout: 15 * 60
        )
    }

    func choosePrivateKey() {
        let panel = NSOpenPanel()
        panel.title = "Choose the App Store Connect private key"
        panel.message = "Choose the .p8 key file. Angel will store it in macOS Keychain and never display it."
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.allowsMultipleSelection = false
        panel.allowedContentTypes = [.data]
        if panel.runModal() == .OK, let url = panel.url {
            privateKeyURL = url
            output = "Private key selected: \(url.lastPathComponent)\nThe key contents are not displayed."
        }
    }

    func saveAppleCredentials() {
        guard let privateKeyURL else {
            output = "Choose the App Store Connect .p8 private key first."
            return
        }

        let didStartAccessing = privateKeyURL.startAccessingSecurityScopedResource()
        defer {
            if didStartAccessing {
                privateKeyURL.stopAccessingSecurityScopedResource()
            }
        }

        do {
            let privateKey = try Data(contentsOf: privateKeyURL, options: [.mappedIfSafe])
            try AngelKeychain.save(
                AppleReleaseCredentials(
                    teamID: appleTeamID.trimmingCharacters(in: .whitespacesAndNewlines),
                    issuerID: appStoreConnectIssuerID.trimmingCharacters(in: .whitespacesAndNewlines),
                    keyID: appStoreConnectKeyID.trimmingCharacters(in: .whitespacesAndNewlines),
                    privateKey: privateKey
                )
            )
            appleCredentialsStatus = "Credentials stored in macOS Keychain."
            output = "Apple release credentials saved to Angel's macOS Keychain entry.\nThey are not written to the project, displayed, or uploaded."
        } catch {
            output = "Could not save Apple credentials: \(error.localizedDescription)"
        }
    }

    func clearAppleCredentials() {
        do {
            try AngelKeychain.delete()
            appleCredentialsStatus = "No credentials stored."
            output = "Angel's stored Apple release credentials were removed from macOS Keychain."
        } catch {
            output = "Could not remove Apple credentials: \(error.localizedDescription)"
        }
    }

    private func setState(_ state: AngelOperationState, for operation: AngelOperation) {
        switch operation {
        case .preview:
            previewState = state
        case .preflightAnalysis:
            preflightAnalysisState = state
        case .importFiles:
            importState = state
        case .analysis:
            analysisState = state
        }
    }

    private func setProjectRoot(_ root: URL) {
        if projectAccessScopeIsActive {
            projectURL?.stopAccessingSecurityScopedResource()
        }
        projectURL = root
        projectAccessScopeIsActive = root.startAccessingSecurityScopedResource()
    }

    nonisolated private static func discoverActivePreflightAnalysis() -> ActivePreflightAnalysis? {
        guard let result = try? CommandRunner.execute(
            executable: URL(fileURLWithPath: "/bin/ps"),
            arguments: ["-axo", "pid=,command="],
            timeout: 5
        ), result.status == 0 else { return nil }

        for rawLine in result.output.split(whereSeparator: \.isNewline) {
            let tokens = rawLine.split(whereSeparator: \.isWhitespace).map(String.init)
            guard let pidText = tokens.first,
                  let processID = Int32(pidText),
                  let moduleIndex = tokens.firstIndex(of: "tools.ingest.cli"),
                  moduleIndex + 1 < tokens.count,
                  tokens.contains("--analyze")
            else { continue }

            func value(after flag: String) -> String? {
                guard let index = tokens.firstIndex(of: flag), index + 1 < tokens.count else { return nil }
                return tokens[index + 1]
            }

            guard let progressPath = value(after: "--progress-file"),
                  let pathsPath = value(after: "--analysis-paths-file"),
                  let catalogPath = value(after: "--catalog")
            else { continue }
            return ActivePreflightAnalysis(
                processID: processID,
                target: tokens[moduleIndex + 1],
                catalogPath: catalogPath,
                pathsURL: URL(fileURLWithPath: pathsPath),
                progressURL: URL(fileURLWithPath: progressPath)
            )
        }
        return nil
    }

    private func mergePreflightPlan(_ incoming: AngelPreviewPlan) -> AngelPreviewPlan {
        guard let current = previewPlan else { return incoming }
        let existingByPath = Dictionary(uniqueKeysWithValues: current.files.map { ($0.sourcePath, $0) })
        let files = incoming.files.map { file in
            guard file.analysis == nil, let existing = existingByPath[file.sourcePath], existing.analysis != nil else {
                return file
            }
            return existing
        }
        return AngelPreviewPlan(
            schemaVersion: incoming.schemaVersion,
            fingerprint: incoming.fingerprint,
            destinationRoot: incoming.destinationRoot,
            catalogPath: incoming.catalogPath,
            summary: incoming.summary,
            analysis: incoming.analysis,
            warnings: incoming.warnings,
            files: files
        )
    }

    private func updatingPreflightPlan(
        _ plan: AngelPreviewPlan,
        status: String,
        message: String
    ) -> AngelPreviewPlan {
        AngelPreviewPlan(
            schemaVersion: plan.schemaVersion,
            fingerprint: plan.fingerprint,
            destinationRoot: plan.destinationRoot,
            catalogPath: plan.catalogPath,
            summary: plan.summary,
            analysis: AngelPlanAnalysis(status: status, message: message),
            warnings: plan.warnings,
            files: plan.files
        )
    }

    private func run(
        operation: AngelOperation,
        runningMessage: String,
        scriptName: String,
        arguments: [String],
        timeout: TimeInterval? = nil,
        cleanupURL: URL? = nil,
        progressURL: URL? = nil
    ) {
        guard !isRunning else {
            return
        }

        guard let projectURL else {
            let message = projectResolver.prompt
            setState(.failed(message), for: operation)
            output = message
            return
        }
        let script = projectURL.appendingPathComponent(scriptName)
        guard FileManager.default.isExecutableFile(atPath: script.path) else {
            let message = "Missing executable: \(script.path)"
            setState(.failed(message), for: operation)
            output = message
            return
        }

        setState(.running(runningMessage), for: operation)
        output = "\(runningMessage)\n\nCommand: \(script.path) \(arguments.joined(separator: " "))"
        progressMonitor?.cancel()
        progressMonitor = progressURL.map { monitorAnalysisProgress(at: $0) }
        Task {
            var completedPreflightBatch = false
            do {
                let result = try await Task.detached(priority: .userInitiated) {
                    try CommandRunner.execute(script: script, arguments: arguments, timeout: timeout)
                }.value

                let commandOutput = result.output.trimmingCharacters(in: .whitespacesAndNewlines)
                if result.status == 0 {
                    if operation.isPlanOperation {
                        do {
                            let decodedPlan = try AngelPreviewPlan.decode(commandOutput)
                            let plan: AngelPreviewPlan
                            if case .preflightAnalysis = operation {
                                plan = mergePreflightPlan(decodedPlan)
                                completedPreflightBatch = true
                            } else {
                                plan = decodedPlan
                            }
                            previewPlan = plan
                            if case .preview = operation {
                                lastPreviewAt = Date()
                                analysisSelection = []
                                approvedFilePaths = []
                            }
                            let message: String
                            if case .preview = operation {
                                message = "Preview completed: \(plan.summaryText)."
                            } else if case .preflightAnalysis = operation {
                                message = "Analysis batch completed. Continuing with the selected files…"
                            } else {
                                message = "Analysis completed: \(plan.analysisMessage)"
                            }
                            setState(.succeeded(message), for: operation)
                            output = renderPreview(plan, message: message)
                        } catch {
                            if case .preview = operation {
                                previewPlan = nil
                                lastPreviewAt = nil
                            }
                            let message = "\(operation.displayName) returned an invalid plan."
                            setState(.failed(message), for: operation)
                            output = "\(message)\n\n\(error.localizedDescription)\n\n\(commandOutput)"
                        }
                    } else {
                        if commandOutput.isEmpty {
                            let message = "\(operation.displayName) completed. No output was returned."
                            setState(.succeeded(message), for: operation)
                            output = message
                        } else {
                            let message = "\(operation.displayName) completed successfully."
                            setState(.succeeded(message), for: operation)
                            output = "\(message)\n\n\(commandOutput)"
                        }
                    }
                } else {
                    if case .preview = operation {
                        previewPlan = nil
                        lastPreviewAt = nil
                    }
                    let message = "\(operation.displayName) failed with exit status \(result.status)."
                    setState(.failed(message), for: operation)
                    output = commandOutput.isEmpty ? message : "\(commandOutput)\n\n\(message)"
                }
            } catch {
                let detail = error.localizedDescription
                let message = detail.isEmpty
                    ? "Could not run \(operation.displayName.lowercased())."
                    : "Could not run \(operation.displayName.lowercased()): \(detail)"
                setState(.failed(message), for: operation)
                output = "\(message)\n\n\(error.localizedDescription)"
            }
            progressMonitor?.cancel()
            progressMonitor = nil
            if let cleanupURL {
                try? FileManager.default.removeItem(at: cleanupURL)
                temporaryPlanFiles.removeAll { $0 == cleanupURL }
            }
            if let progressURL {
                try? FileManager.default.removeItem(at: progressURL)
                temporaryPlanFiles.removeAll { $0 == progressURL }
            }
            if completedPreflightBatch {
                advancePreflightAnalysis()
            }
        }
    }

    private func monitorAnalysisProgress(at url: URL) -> Task<Void, Never> {
        Task { [weak self] in
            var offset = 0
            var partialLine = ""
            while !Task.isCancelled {
                if let data = try? Data(contentsOf: url), data.count > offset {
                    let newData = data.subdata(in: offset..<data.count)
                    offset = data.count
                    partialLine += String(decoding: newData, as: UTF8.self)
                    let lines = partialLine.split(separator: "\n", omittingEmptySubsequences: false)
                    partialLine = String(lines.last ?? "")
                    for line in lines.dropLast() where !line.isEmpty {
                        guard let progress = try? AngelAnalysisProgress.decode(String(line)) else { continue }
                        self?.applyAnalysisProgress(progress)
                    }
                }
                try? await Task.sleep(nanoseconds: 250_000_000)
            }
        }
    }

    private func applyAnalysisProgress(_ progress: AngelAnalysisProgress) {
        if progress.event == "analysis_started" {
            guard let currentPlan = previewPlan else { return }
            analysisCurrentSourcePath = progress.sourcePath
            let message = "Analyzing \(preflightBatchCompletedBase + progress.completed + 1) of \(preflightAnalysisTotal) selected file(s)."
            setState(.running(message), for: .preflightAnalysis)
            output = renderPreview(currentPlan, message: message)
            return
        }

        guard let analyzedFile = progress.file,
              let currentPlan = previewPlan,
              let index = currentPlan.files.firstIndex(where: { $0.sourcePath == progress.sourcePath })
        else { return }

        var files = currentPlan.files
        files[index] = analyzedFile
        analysisCurrentSourcePath = nil
        analysisLatestSourcePath = progress.sourcePath
        if !analysisHistorySourcePaths.contains(progress.sourcePath) {
            analysisHistorySourcePaths.append(progress.sourcePath)
        }
        let nextIndex = preflightBatchCompletedBase + progress.completed
        if nextIndex < preflightSelectedPaths.count {
            // Older workers did not emit an explicit started event. Infer the
            // next image from the ordered paths file so reconnecting to an
            // already-running job still shows what is being analyzed now.
            analysisCurrentSourcePath = preflightSelectedPaths[nextIndex]
        }
        let message = "Analyzed \(preflightBatchCompletedBase + progress.completed) of \(preflightAnalysisTotal) selected file(s). Review results as they arrive."
        let updatedPlan = AngelPreviewPlan(
            schemaVersion: currentPlan.schemaVersion,
            fingerprint: currentPlan.fingerprint,
            destinationRoot: currentPlan.destinationRoot,
            catalogPath: currentPlan.catalogPath,
            summary: currentPlan.summary,
            analysis: AngelPlanAnalysis(status: "running", message: message),
            warnings: currentPlan.warnings,
            files: files
        )
        previewPlan = updatedPlan
        setState(.running(message), for: .preflightAnalysis)
        output = renderPreview(previewPlan!, message: message)
    }

    private func renderPreview(_ plan: AngelPreviewPlan, message: String) -> String {
        var lines = [
            message,
            "",
            plan.summaryText,
            "Destination root: \(plan.destinationRoot)",
            "Plan fingerprint: \(plan.fingerprint)",
            "Analysis: \(plan.analysis.message)",
        ]
        if !plan.warnings.isEmpty {
            lines.append("")
            lines.append(contentsOf: plan.warnings.map { "Warning: \($0)" })
        }
        if !plan.files.isEmpty {
            lines.append("")
            lines.append("Files:")
            for file in plan.files {
                lines.append("[\(file.status)] \(file.currentFilename) → \(file.landingDestination)")
                if let warning = file.warning {
                    lines.append("  Warning: \(warning)")
                }
            }
        }
        return lines.joined(separator: "\n")
    }

    private func readRegisteredSDVolumeIDs() -> Set<String> {
        guard let projectURL else { return [] }
        let configURL = projectURL.appendingPathComponent("config/ingest.yaml")
        guard let contents = try? String(contentsOf: configURL, encoding: .utf8) else { return [] }

        var inSources = false
        var ids = Set<String>()
        for line in contents.components(separatedBy: .newlines) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed == "sources:" {
                inSources = true
                continue
            }
            if inSources && !line.isEmpty && !line.hasPrefix(" ") && !line.hasPrefix("\t") {
                inSources = false
            }
            guard inSources, trimmed.hasPrefix("-") else { continue }
            let value = trimmed.dropFirst().trimmingCharacters(in: .whitespaces)
                .trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
            if value.count >= 8 {
                ids.insert(String(value))
            }
        }
        return ids
    }

    private func readConfiguredT7VolumeID() -> String? {
        guard let projectURL else { return nil }
        let configURL = projectURL.appendingPathComponent("config/ingest.yaml")
        guard let contents = try? String(contentsOf: configURL, encoding: .utf8) else { return nil }

        var inT7 = false
        for line in contents.components(separatedBy: .newlines) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed == "t7:" {
                inT7 = true
                continue
            }
            if inT7 && !line.isEmpty && !line.hasPrefix(" ") && !line.hasPrefix("\t") {
                inT7 = false
            }
            guard inT7, trimmed.hasPrefix("volume_uuid:") else { continue }
            let value = trimmed.dropFirst("volume_uuid:".count)
                .trimmingCharacters(in: .whitespaces)
                .trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
            guard !value.isEmpty, !value.hasPrefix("REPLACE-WITH") else { return nil }
            return String(value)
        }
        return nil
    }

    nonisolated private static func probeRemovableVolumes() -> [AngelMountedVolume] {
        let fileManager = FileManager.default
        let volumesDirectory = URL(fileURLWithPath: "/Volumes", isDirectory: true)
        guard let entries = try? fileManager.contentsOfDirectory(
            at: volumesDirectory,
            includingPropertiesForKeys: [.isDirectoryKey],
            options: [.skipsHiddenFiles]
        ) else {
            return []
        }

        return entries.compactMap { entry in
            guard let result = try? CommandRunner.execute(
                executable: URL(fileURLWithPath: "/usr/sbin/diskutil"),
                arguments: ["info", "-plist", entry.path],
                timeout: 15
            ), result.status == 0,
            let data = result.output.data(using: .utf8),
            let plist = try? PropertyListSerialization.propertyList(from: data, options: [], format: nil),
            let info = plist as? [String: Any],
            let uuid = info["VolumeUUID"] as? String
            else {
                return nil
            }

            let removable = (info["RemovableMedia"] as? Bool ?? false) || (info["Ejectable"] as? Bool ?? false)
            guard removable else { return nil }
            let name = (info["VolumeName"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? entry.lastPathComponent
            return AngelMountedVolume(
                id: uuid,
                name: name,
                mountPath: (info["MountPoint"] as? String) ?? entry.path,
                writable: info["WritableVolume"] as? Bool ?? false
            )
        }.sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
    }

    private static func registerSDVolumeID(_ volumeID: String, projectRoot: URL) throws {
        let fileManager = FileManager.default
        let configDirectory = projectRoot.appendingPathComponent("config", isDirectory: true)
        let configURL = configDirectory.appendingPathComponent("ingest.yaml")
        if !fileManager.fileExists(atPath: configURL.path) {
            let exampleURL = configDirectory.appendingPathComponent("ingest.example.yaml")
            guard fileManager.fileExists(atPath: exampleURL.path) else {
                throw NSError(domain: "Angel", code: 1, userInfo: [NSLocalizedDescriptionKey: "Angel could not find its ingestion configuration template."])
            }
            try fileManager.copyItem(at: exampleURL, to: configURL)
        }

        var lines = try String(contentsOf: configURL, encoding: .utf8).components(separatedBy: .newlines)
        if lines.contains(where: { $0.contains("- \"\(volumeID)\"") || $0.contains("- '\(volumeID)'") }) {
            return
        }

        guard let sourcesIndex = lines.firstIndex(where: { $0.trimmingCharacters(in: .whitespaces) == "sources:" }) else {
            throw NSError(domain: "Angel", code: 2, userInfo: [NSLocalizedDescriptionKey: "Angel could not find the sources section in its configuration."])
        }
        guard let uuidIndex = lines.indices.drop(while: { $0 <= sourcesIndex }).first(where: { lines[$0].contains("sd_volume_uuids:") }) else {
            throw NSError(domain: "Angel", code: 3, userInfo: [NSLocalizedDescriptionKey: "Angel could not find the SD-card setting in its configuration."])
        }

        if lines[uuidIndex].contains("sd_volume_uuids: []") {
            lines[uuidIndex] = "  sd_volume_uuids:\n    - \"\(volumeID)\""
        } else {
            lines.insert("    - \"\(volumeID)\"", at: uuidIndex + 1)
        }
        try lines.joined(separator: "\n").write(to: configURL, atomically: true, encoding: .utf8)
    }
}

struct AngelOperationStatusView: View {
    let state: AngelOperationState
    let operationName: String
    let retryAction: (() -> Void)?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var tint: Color {
        switch state {
        case .ready, .running:
            .secondary
        case .succeeded:
            .green
        case .failed:
            .red
        }
    }

    private var displayedStatusText: String {
        if case .ready = state {
            switch operationName {
            case "Preview":
                return "Ready to preview changes."
            case "Import":
                return "Ready to move the selected files."
            default:
                return "Choose a catalog and asset to analyze."
            }
        }
        return state.statusText
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .top, spacing: 8) {
                if state.isRunning {
                    ProgressView()
                        .controlSize(.small)
                        .accessibilityLabel("\(operationName) in progress")
                } else {
                    Image(systemName: state.symbolName)
                        .foregroundStyle(tint)
                }

                Text(displayedStatusText)
                    .font(.callout)
                    .foregroundStyle(tint)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if case .failed = state, let retryAction {
                Button("Try again", action: retryAction)
                    .buttonStyle(.link)
                    .padding(.leading, 24)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(operationName) status: \(displayedStatusText)")
        .animation(reduceMotion ? nil : .easeOut(duration: 0.18), value: state)
    }
}

struct AngelThumbnailView: View {
    let path: String
    let size: CGFloat
    @State private var image: NSImage?
    @State private var loadFailed = false

    init(path: String, size: CGFloat = 72) {
        self.path = path
        self.size = size
    }

    var body: some View {
        Group {
            if let image {
                Image(nsImage: image)
                    .resizable()
                    .scaledToFill()
            } else {
                Image(systemName: loadFailed ? "photo.badge.exclamationmark" : "photo")
                    .font(.title2)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .frame(width: size, height: size)
        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 6))
        .clipShape(RoundedRectangle(cornerRadius: 6))
        .task(id: path) {
            image = nil
            loadFailed = false
            let data = await Task.detached(priority: .utility) {
                try? Data(contentsOf: URL(fileURLWithPath: path), options: [.mappedIfSafe])
            }.value
            guard !Task.isCancelled else { return }
            guard let data else {
                loadFailed = true
                return
            }
            image = AngelThumbnailDecoder.decode(data)
            loadFailed = image == nil
        }
        .accessibilityLabel(
            loadFailed
                ? "Preview unavailable for \(URL(fileURLWithPath: path).lastPathComponent)"
                : "Preview thumbnail for \(URL(fileURLWithPath: path).lastPathComponent)"
        )
    }
}

enum AngelThumbnailDecoder {
    static func decode(_ data: Data, maxPixelSize: Int = 512) -> NSImage? {
        guard !data.isEmpty,
              let source = CGImageSourceCreateWithData(data as CFData, nil)
        else {
            return nil
        }

        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: max(1, maxPixelSize),
        ]

        guard let cgImage = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary),
              cgImage.width > 0,
              cgImage.height > 0
        else {
            return nil
        }

        return NSImage(
            cgImage: cgImage,
            size: NSSize(width: cgImage.width, height: cgImage.height)
        )
    }
}

struct AngelLiveAnalysisView: View {
    let currentFile: AngelPlanFile?
    let latestFile: AngelPlanFile?
    let statusText: String

    private var hasContent: Bool {
        currentFile != nil || latestFile != nil
    }

    var body: some View {
        if hasContent {
            GroupBox("Live analysis") {
                VStack(alignment: .leading, spacing: 10) {
                    if let currentFile {
                        HStack(alignment: .top, spacing: 10) {
                            AngelThumbnailView(path: currentFile.sourcePath, size: 120)
                            VStack(alignment: .leading, spacing: 4) {
                                Label("Currently analyzing", systemImage: "eye.circle")
                                    .font(.callout.weight(.semibold))
                                    .foregroundStyle(.blue)
                                Text(currentFile.currentFilename)
                                    .font(.headline)
                                Text(currentFile.sourcePath)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .lineLimit(2)
                                    .truncationMode(.middle)
                                Text(statusText)
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }

                    if let latestFile {
                        if currentFile != nil {
                            Divider()
                        }
                        HStack(alignment: .top, spacing: 10) {
                            AngelThumbnailView(path: latestFile.sourcePath, size: 120)
                            VStack(alignment: .leading, spacing: 4) {
                                Label("Latest completed result", systemImage: "checkmark.circle")
                                    .font(.callout.weight(.semibold))
                                    .foregroundStyle(.green)
                                Text(latestFile.currentFilename)
                                    .font(.headline)
                                if let analysis = latestFile.analysis {
                                    if let summary = analysis.summary, !summary.isEmpty {
                                        Text(summary)
                                            .font(.caption)
                                            .fixedSize(horizontal: false, vertical: true)
                                    }
                                    if let people = analysis.peopleText {
                                        Label("People/profile matches: \(people)", systemImage: "person.2")
                                            .font(.caption)
                                            .foregroundStyle(.blue)
                                    } else if let people = analysis.people, !people.isEmpty {
                                        Label("People detected; no existing profile match", systemImage: "person.crop.circle.badge.questionmark")
                                            .font(.caption)
                                            .foregroundStyle(.orange)
                                    }
                                    if let animals = analysis.animalsText {
                                        Label("Animals: \(animals)", systemImage: "pawprint")
                                            .font(.caption)
                                            .foregroundStyle(.blue)
                                    }
                                    if let error = analysis.error, !error.isEmpty {
                                        Text("Analysis issue: \(error)")
                                            .font(.caption)
                                            .foregroundStyle(.orange)
                                            .fixedSize(horizontal: false, vertical: true)
                                    }
                                    Text("Proposal: \(latestFile.proposedFilename)")
                                        .font(.caption.weight(.medium))
                                    Text(latestFile.landingDestination)
                                        .font(.system(.caption, design: .monospaced))
                                        .foregroundStyle(.secondary)
                                        .lineLimit(2)
                                }
                            }
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(4)
            }
        }
    }
}

struct AngelAnalysisHistoryRow: View {
    let file: AngelPlanFile

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            AngelThumbnailView(path: file.sourcePath)
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline) {
                    Text(file.currentFilename)
                        .font(.callout.weight(.medium))
                    Spacer()
                    Label(
                        file.analysis?.status == "ready_for_review" ? "Ready to review" : "Needs attention",
                        systemImage: file.analysis?.status == "ready_for_review" ? "checkmark.circle" : "exclamationmark.triangle"
                    )
                    .font(.caption)
                    .foregroundStyle(file.analysis?.status == "ready_for_review" ? .green : .orange)
                }
                if let analysis = file.analysis {
                    if let summary = analysis.summary, !summary.isEmpty {
                        Text(summary)
                            .font(.caption)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    HStack(spacing: 12) {
                        if let people = analysis.peopleText {
                            Label(people, systemImage: "person.2")
                        } else if let people = analysis.people, !people.isEmpty {
                            Label("People detected", systemImage: "person.crop.circle.badge.questionmark")
                        }
                        if let animals = analysis.animalsText {
                            Label(animals, systemImage: "pawprint")
                        }
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    if let error = analysis.error, !error.isEmpty {
                        Text(error)
                            .font(.caption)
                            .foregroundStyle(.orange)
                    } else {
                        Text("Proposal: \(file.proposedFilename)")
                            .font(.caption.weight(.medium))
                    }
                }
            }
        }
        .padding(8)
        .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 6))
    }
}

struct AngelAnalysisMonitorView: View {
    @Bindable var model: AngelModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Analysis Monitor")
                        .font(.title2.bold())
                    Text("Watch the current image and review results as Ollama completes them.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button("Reconnect") {
                    model.reconnectToActivePreflightAnalysis()
                }
                if model.preflightAnalysisState.isRunning {
                    ProgressView()
                        .controlSize(.small)
                }
            }

            Label(model.preflightAnalysisState.statusText, systemImage: model.preflightAnalysisState.isRunning ? "sparkles" : "info.circle")
                .font(.callout)
                .foregroundStyle(model.preflightAnalysisState.isRunning ? .blue : .secondary)
                .fixedSize(horizontal: false, vertical: true)

            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    if model.currentAnalysisFile == nil && model.latestAnalysisFile == nil {
                        GroupBox {
                            Label(
                                "Start pre-ingest analysis from the main Angel window to see images and results here.",
                                systemImage: "photo.on.rectangle.angled"
                            )
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                        }
                    }

                    AngelLiveAnalysisView(
                        currentFile: model.currentAnalysisFile,
                        latestFile: model.latestAnalysisFile,
                        statusText: model.preflightAnalysisState.statusText
                    )

                    GroupBox("Completed images (\(model.analysisHistoryFiles.count))") {
                        if model.analysisHistoryFiles.isEmpty {
                            Text("No completed image results yet.")
                                .font(.callout)
                                .foregroundStyle(.secondary)
                        } else {
                            LazyVStack(alignment: .leading, spacing: 8) {
                                ForEach(model.analysisHistoryFiles) { file in
                                    AngelAnalysisHistoryRow(file: file)
                                }
                            }
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(20)
        .frame(minWidth: 680, minHeight: 520)
    }
}

struct ContentView: View {
    @Bindable var model: AngelModel
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Angel")
                        .font(.largeTitle.bold())
                    Text("Safe local media organization and analysis")
                        .foregroundStyle(.secondary)
                }
                Spacer()
            }

            GroupBox("Connected storage") {
                VStack(alignment: .leading, spacing: 10) {
                    HStack(alignment: .firstTextBaseline) {
                        Text(model.storageStatus)
                            .font(.callout)
                        Spacer()
                        Button {
                            model.refreshStorage()
                        } label: {
                            HStack(spacing: 6) {
                                if model.isRefreshingStorage {
                                    ProgressView()
                                        .controlSize(.small)
                                }
                                Text(model.isRefreshingStorage ? "Checking…" : "Refresh")
                            }
                        }
                        .disabled(model.isRefreshingStorage)
                    }

                    if model.mountedRemovableVolumes.isEmpty {
                        Label(
                            "Insert an SD card, then click Refresh.",
                            systemImage: "sdcard"
                        )
                        .foregroundStyle(.secondary)
                    } else {
                        ForEach(model.mountedRemovableVolumes) { volume in
                            HStack(spacing: 10) {
                                Image(systemName: "sdcard.fill")
                                    .foregroundStyle(.secondary)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(volume.name)
                                        .font(.headline)
                                    Text(volume.displayPath)
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                                Spacer()
                                if model.registeredSDVolumeIDs.contains(volume.id) {
                                    Label("Ready", systemImage: "checkmark.circle.fill")
                                        .foregroundStyle(.green)
                                } else if volume.writable {
                                    Button("Use this card") {
                                        model.registerSDVolume(volume)
                                    }
                                } else {
                                    Text("Read-only")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(4)
            }

            GroupBox("Organize") {
                VStack(alignment: .leading, spacing: 12) {
                    HStack(alignment: .top, spacing: 12) {
                        VStack(alignment: .leading, spacing: 4) {
                            Label("Project folder", systemImage: model.projectURL == nil ? "folder" : "folder.fill")
                                .foregroundStyle(.secondary)
                            Text(model.projectURL?.path ?? "No valid project folder selected")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                                .truncationMode(.middle)
                        }
                        Spacer()
                        Button("Choose Project Folder…", action: model.chooseProject)
                    }

                    Picker("Source", selection: $model.selectedTarget) {
                        ForEach(AngelTarget.allCases) { target in
                            Text(target.title).tag(target)
                        }
                    }
                    .pickerStyle(.menu)

                    HStack(alignment: .center, spacing: 12) {
                        Button {
                            model.preview()
                        } label: {
                            HStack(spacing: 8) {
                                if model.previewState.isRunning {
                                    ProgressView()
                                        .controlSize(.small)
                                }
                                Text(model.previewState.isRunning ? "Scanning…" : "Refresh preview")
                            }
                        }
                        .disabled(model.isRunning || model.projectURL == nil)
                        .accessibilityLabel("Refresh preview")
                        .accessibilityHint("Preview changes without modifying files.")
                        .keyboardShortcut(.defaultAction)
                        Text(model.previewState.isRunning ? "Scanning the card…" : model.previewStatusText)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    AngelOperationStatusView(
                        state: model.previewState,
                        operationName: "Preview",
                        retryAction: { model.preview() }
                    )

                    GroupBox("Review plan") {
                        if let plan = model.previewPlan {
                            VStack(alignment: .leading, spacing: 8) {
                                Text(plan.summaryText)
                                    .font(.callout.weight(.semibold))
                                Text("Destination: \(plan.destinationRoot)")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .textSelection(.enabled)
                                Text("Fingerprint: \(plan.fingerprint)")
                                    .font(.system(.caption, design: .monospaced))
                                    .foregroundStyle(.secondary)
                                    .textSelection(.enabled)
                                Label(plan.analysis.message, systemImage: "sparkles")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                                    .fixedSize(horizontal: false, vertical: true)

                                HStack(spacing: 8) {
                                    TextField("Filter files by name or path", text: $model.reviewFilter)
                                        .textFieldStyle(.roundedBorder)
                                    Text("Showing \(model.filteredPreviewFiles.count) of \(plan.files.count)")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                        .fixedSize()
                                }

                                HStack(spacing: 8) {
                                    Button("Select visible for analysis") {
                                        model.selectAllForAnalysis()
                                    }
                                    Button {
                                        model.analyzeSelectedPreviewFiles()
                                    } label: {
                                        HStack(spacing: 6) {
                                            if model.preflightAnalysisState.isRunning {
                                                ProgressView()
                                                    .controlSize(.small)
                                            }
                                            Text(
                                                model.preflightAnalysisState.isRunning
                                                    ? "Analyzing…"
                                                    : "Analyze selected (\(model.analysisSelection.count))"
                                            )
                                        }
                                    }
                                    .disabled(
                                        model.isRunning ||
                                        model.analysisSelection.isEmpty ||
                                        plan.catalogPath == nil
                                    )
                                    Button {
                                        model.approveAndRenameAll()
                                    } label: {
                                        Label(
                                            "Approve & rename all (\(model.reviewableFileCount))",
                                            systemImage: "checkmark.circle"
                                        )
                                    }
                                    .buttonStyle(.borderedProminent)
                                    .disabled(model.isRunning || model.reviewableFileCount == 0)
                                    .accessibilityLabel("Approve and rename all reviewed files")
                                    .accessibilityHint("Stages every analyzed proposal. The Move and rename approved files button applies them after verification.")
                                    Button("Open Analysis Monitor") {
                                        openWindow(id: "analysis-monitor")
                                    }
                                    Button("Reconnect active run") {
                                        model.reconnectToActivePreflightAnalysis()
                                    }
                                }
                                .font(.caption)
                                if model.reviewableFileCount > 0 {
                                    Text("Approved \(model.approvedReviewableFileCount) of \(model.reviewableFileCount) reviewed files. Approval stages the plan; the final move applies the reviewed names after verification.")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                                if plan.catalogPath == nil {
                                    Text("Connect the T7 and refresh preview to enable local people/animal analysis.")
                                        .font(.caption)
                                        .foregroundStyle(.orange)
                                }
                                AngelOperationStatusView(
                                    state: model.preflightAnalysisState,
                                    operationName: "Pre-ingest analysis",
                                    retryAction: { model.analyzeSelectedPreviewFiles() }
                                )
                                AngelLiveAnalysisView(
                                    currentFile: model.currentAnalysisFile,
                                    latestFile: model.latestAnalysisFile,
                                    statusText: model.preflightAnalysisState.statusText
                                )

                                if plan.files.isEmpty {
                                    Text("No eligible files were found on this source.")
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                } else {
                                    ScrollView {
                                        LazyVStack(alignment: .leading, spacing: 8) {
                                            ForEach(model.filteredPreviewFiles) { file in
                                                HStack(alignment: .top, spacing: 10) {
                                                    AngelThumbnailView(path: file.sourcePath)
                                                    VStack(alignment: .leading, spacing: 5) {
                                                        HStack(alignment: .firstTextBaseline, spacing: 6) {
                                                            Image(
                                                                systemName: file.status == "ready_for_review"
                                                                    ? "checkmark.circle"
                                                                    : "exclamationmark.triangle"
                                                            )
                                                            .foregroundStyle(
                                                                file.status == "ready_for_review" ? .green : .orange
                                                            )
                                                            Text(file.currentFilename)
                                                                .font(.callout.weight(.medium))
                                                            Spacer()
                                                            Text(file.sourceClass.uppercased())
                                                                .font(.caption2.weight(.semibold))
                                                                .foregroundStyle(.secondary)
                                                        }
                                                        Toggle(
                                                            "Include in analysis",
                                                            isOn: Binding(
                                                                get: { model.analysisSelection.contains(file.sourcePath) },
                                                                set: {
                                                                    model.toggleAnalysisSelection(
                                                                        for: file.sourcePath,
                                                                        selected: $0
                                                                    )
                                                                }
                                                            )
                                                        )
                                                        .toggleStyle(.checkbox)
                                                        .font(.caption)
                                                        Text("Current: \(file.currentFilename)")
                                                            .font(.caption)
                                                            .foregroundStyle(.secondary)
                                                        Text("Proposed name: \(file.proposedFilename)")
                                                            .font(.caption.weight(.medium))
                                                        Text("Destination: \(file.landingDestination)")
                                                            .font(.system(.caption, design: .monospaced))
                                                            .textSelection(.enabled)

                                                        if let analysis = file.analysis {
                                                            if let summary = analysis.summary, !summary.isEmpty {
                                                                Text(summary)
                                                                    .font(.caption)
                                                                    .foregroundStyle(.secondary)
                                                                    .fixedSize(horizontal: false, vertical: true)
                                                            }
                                                            if let people = analysis.peopleText {
                                                                Label("People/profile matches: \(people)", systemImage: "person.2")
                                                                    .font(.caption)
                                                                    .foregroundStyle(.blue)
                                                            } else if let people = analysis.people, !people.isEmpty {
                                                                Label(
                                                                    "People detected; no existing profile match",
                                                                    systemImage: "person.crop.circle.badge.questionmark"
                                                                )
                                                                .font(.caption)
                                                                .foregroundStyle(.orange)
                                                            }
                                                            if let animals = analysis.animalsText {
                                                                Label("Animals: \(animals)", systemImage: "pawprint")
                                                                    .font(.caption)
                                                                    .foregroundStyle(.blue)
                                                            }
                                                            if let collisionResolution = analysis.collisionResolution {
                                                                Label(collisionResolution, systemImage: "arrow.triangle.branch")
                                                                    .font(.caption)
                                                                    .foregroundStyle(.orange)
                                                                    .fixedSize(horizontal: false, vertical: true)
                                                            }
                                                            Toggle(
                                                                "Approve this move",
                                                                isOn: Binding(
                                                                    get: { model.approvedFilePaths.contains(file.sourcePath) },
                                                                    set: {
                                                                        model.toggleApproval(for: file, approved: $0)
                                                                    }
                                                                )
                                                            )
                                                            .toggleStyle(.checkbox)
                                                            .disabled(!analysis.isReadyForReview)
                                                            .font(.caption.weight(.semibold))
                                                        } else {
                                                            Text("Not analyzed yet. No people, animal, rename, or folder proposal exists.")
                                                                .font(.caption)
                                                                .foregroundStyle(.secondary)
                                                                .fixedSize(horizontal: false, vertical: true)
                                                        }
                                                        if let warning = file.warning {
                                                            Text(warning)
                                                                .font(.caption)
                                                                .foregroundStyle(.orange)
                                                                .fixedSize(horizontal: false, vertical: true)
                                                        }
                                                    }
                                                }
                                                .padding(8)
                                                .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 6))
                                            }
                                        }
                                    }
                                    .frame(maxHeight: 220)
                                }
                                if !plan.warnings.isEmpty {
                                    ForEach(plan.warnings, id: \.self) { warning in
                                        Label(warning, systemImage: "exclamationmark.triangle")
                                            .font(.caption)
                                            .foregroundStyle(.orange)
                                            .fixedSize(horizontal: false, vertical: true)
                                    }
                                }
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                        } else {
                            Label(
                                "Refresh preview to generate a reviewed, per-file plan before moving anything.",
                                systemImage: "doc.text.magnifyingglass"
                            )
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                        }
                    }

                    HStack(alignment: .center, spacing: 12) {
                        Button {
                            model.requestImport()
                        } label: {
                            HStack(spacing: 8) {
                                if model.importState.isRunning {
                                    ProgressView()
                                        .controlSize(.small)
                                }
                                Text(model.importState.isRunning ? "Moving…" : "Move & rename approved files")
                            }
                        }
                        .disabled(!model.canImport)
                        .accessibilityLabel("Move and rename approved files")
                        .accessibilityHint("Copies approved files to the Angel drive, verifies them, then removes the originals.")
                        Text("Originals are removed only after verification.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    AngelOperationStatusView(
                        state: model.importState,
                        operationName: "Import",
                        retryAction: { model.requestImport() }
                    )
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(4)
            }

            GroupBox("After ingest: model analysis") {
                VStack(alignment: .leading, spacing: 12) {
                    HStack {
                        Button("Choose Catalog…", action: model.chooseCatalog)
                        Text(model.catalogURL?.lastPathComponent ?? "No catalog selected")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    HStack {
                        Button("Choose Asset…", action: model.chooseAsset)
                        Text(model.assetURL?.lastPathComponent ?? "No asset selected")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Button {
                        model.analyze()
                    } label: {
                        HStack(spacing: 8) {
                            if model.analysisState.isRunning {
                                ProgressView()
                                    .controlSize(.small)
                            }
                            Text(model.analysisState.isRunning ? "Analyzing…" : "Analyze asset")
                        }
                    }
                    .disabled(
                        model.catalogURL == nil ||
                        model.assetURL == nil ||
                        model.isRunning
                    )
                    AngelOperationStatusView(
                        state: model.analysisState,
                        operationName: "Asset analysis",
                        retryAction: { model.analyze() }
                    )
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(4)
            }

            GroupBox("Apple release credentials") {
                VStack(alignment: .leading, spacing: 10) {
                    Text("For future signing and release automation only. Angel does not upload anything from this screen.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    TextField("Apple Developer Team ID", text: $model.appleTeamID)
                        .textFieldStyle(.roundedBorder)
                    TextField("App Store Connect Issuer ID", text: $model.appStoreConnectIssuerID)
                        .textFieldStyle(.roundedBorder)
                    TextField("App Store Connect Key ID", text: $model.appStoreConnectKeyID)
                        .textFieldStyle(.roundedBorder)
                    HStack {
                        Button("Choose .p8 private key…", action: model.choosePrivateKey)
                        Text(model.privateKeyURL?.lastPathComponent ?? "No private key selected")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    HStack {
                        Button("Save to Keychain", action: model.saveAppleCredentials)
                        Button("Remove stored credentials", role: .destructive, action: model.clearAppleCredentials)
                        Text(model.appleCredentialsStatus)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(4)
            }

            Text("Output")
                .font(.headline)
            ScrollView {
                Text(model.output)
                    .font(.system(.body, design: .monospaced))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .topLeading)
                    .padding(12)
            }
            .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 8))
        }
        .padding(24)
        .frame(minWidth: 680, minHeight: 520)
        .confirmationDialog(
            "Move and rename approved files from \(model.selectedTarget.title.lowercased())?",
            isPresented: $model.showImportConfirmation,
            titleVisibility: .visible
        ) {
            Button("Move & rename approved files", role: .destructive) {
                model.importFiles()
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Angel will copy each approved file to its reviewed destination and name, verify it on the Angel drive, then remove its original. Files that fail verification stay where they are.")
        }
    }
}
