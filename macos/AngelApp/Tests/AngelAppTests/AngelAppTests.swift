import Foundation
import Testing
@testable import AngelApp

@MainActor
struct AngelAppTests {
    @Test
    func thumbnailDecoderCreatesAThumbnailFromValidImageData() {
        let onePixelPNG = Data(base64Encoded: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")!

        let image = AngelThumbnailDecoder.decode(onePixelPNG)

        #expect(image != nil)
        #expect(image?.size.width == 1)
        #expect(image?.size.height == 1)
    }

    @Test
    func thumbnailDecoderRejectsMissingOrCorruptImageData() {
        #expect(AngelThumbnailDecoder.decode(Data()) == nil)
        #expect(AngelThumbnailDecoder.decode(Data("not an image".utf8)) == nil)
    }

    @Test
    func commandRunnerDrainsLargeOutputBeforeWaitingForProcessExit() throws {
        let result = try CommandRunner.execute(
            executable: URL(fileURLWithPath: "/bin/sh"),
            arguments: ["-c", "dd if=/dev/zero bs=1024 count=128 2>/dev/null"],
            timeout: 5
        )

        #expect(result.status == 0)
        #expect(result.output.utf8.count == 128 * 1024)
    }

    @Test
    func commandRunnerStopsAHelperThatExceedsItsTimeout() throws {
        do {
            _ = try CommandRunner.execute(
                executable: URL(fileURLWithPath: "/bin/sh"),
                arguments: ["-c", "sleep 2"],
                timeout: 0.1
            )
            Issue.record("Expected the helper to time out.")
        } catch let error as CommandRunnerError {
            #expect(error == .timedOut(seconds: 1))
        }
    }

    @Test
    func previewPlanDecodesPerFileDestinationAndHonestAnalysisState() throws {
        let json = """
        {
          "schema_version": 1,
          "fingerprint": "abc123",
          "destination_root": "/Volumes/T7/00-inbox/organized",
          "summary": {"file_count": 1, "total_bytes": 42, "warning_count": 1},
          "analysis": {
            "status": "not_run",
            "message": "Analysis has not run yet. Select files and run pre-ingest analysis to see people, animals, profile matches, and move proposals."
          },
          "warnings": [],
          "files": [{
            "source_path": "/Volumes/CARD/DCIM/IMG_0001.JPG",
            "relative_path": "DCIM/IMG_0001.JPG",
            "source_class": "sd",
            "source_volume": "CARD-UUID",
            "current_filename": "IMG_0001.JPG",
            "proposed_filename": "IMG_0001.JPG",
            "landing_destination": "/Volumes/T7/00-inbox/organized/sd/DCIM/IMG_0001.JPG",
            "status": "ready",
            "warning": null,
            "size": 42,
            "mtime_ns": 123
          }]
        }
        """

        let plan = try AngelPreviewPlan.decode(json)

        #expect(plan.fingerprint == "abc123")
        #expect(plan.summary.fileCount == 1)
        #expect(plan.files.first?.landingDestination == "/Volumes/T7/00-inbox/organized/sd/DCIM/IMG_0001.JPG")
        #expect(plan.analysis.status == "not_run")
        #expect(plan.analysis.message.contains("pre-ingest analysis"))
    }

    @Test
    func onlyReviewedRowsCanBeApprovedForMove() {
        let resolver = AngelProjectRootResolver(knownRoots: [])
        let model = AngelModel(projectResolver: resolver)
        let ready = AngelPlanFile(
            sourcePath: "/Volumes/CARD/ready.jpg",
            relativePath: "ready.jpg",
            sourceClass: "sd",
            sourceVolume: "CARD",
            currentFilename: "ready.jpg",
            proposedFilename: "ready-renamed.jpg",
            landingDestination: "/Volumes/T7/ready-renamed.jpg",
            status: "ready_for_review",
            warning: "AI proposal only.",
            size: 10,
            mtimeNs: 1,
            analysis: AngelFileAnalysis(
                status: "ready_for_review",
                summary: "review me",
                people: nil,
                profileMatches: nil,
                animals: nil,
                tags: nil,
                filenameProposal: nil,
                proposedFilename: "ready-renamed.jpg",
                destinationRelativePath: "ready-renamed.jpg",
                collisionResolution: nil,
                error: nil
            )
        )
        let notReady = AngelPlanFile(
            sourcePath: "/Volumes/CARD/not-ready.jpg",
            relativePath: "not-ready.jpg",
            sourceClass: "sd",
            sourceVolume: "CARD",
            currentFilename: "not-ready.jpg",
            proposedFilename: "not-ready.jpg",
            landingDestination: "/Volumes/T7/not-ready.jpg",
            status: "ready",
            warning: nil,
            size: 10,
            mtimeNs: 2,
            analysis: nil
        )
        model.previewPlan = AngelPreviewPlan(
            schemaVersion: 1,
            fingerprint: "stable",
            destinationRoot: "/Volumes/T7",
            catalogPath: "/Volumes/T7/catalog.sqlite",
            summary: AngelPlanSummary(fileCount: 2, totalBytes: 20, warningCount: 1),
            analysis: AngelPlanAnalysis(status: "complete", message: "Review proposals."),
            warnings: [],
            files: [ready, notReady]
        )

        model.toggleApproval(for: notReady, approved: true)
        #expect(model.approvedFilePaths.isEmpty)

        model.approveAndRenameAll()
        #expect(model.approvedFilePaths == [ready.sourcePath])
        #expect(model.approvedReviewableFileCount == 1)
    }

    @Test
    func previewWithoutProjectShowsAdjacentRecoveryState() {
        let resolver = AngelProjectRootResolver(knownRoots: [])
        let model = AngelModel(projectResolver: resolver)

        model.preview()

        #expect(model.previewState == .failed(resolver.prompt))
        #expect(model.output == resolver.prompt)
    }

    @Test
    func resolverPrefersSavedBookmarkOverKnownWorkspace() throws {
        let fileManager = FileManager.default
        let savedRoot = try makeProjectRoot(fileManager: fileManager)
        let knownRoot = try makeProjectRoot(fileManager: fileManager)
        defer {
            try? fileManager.removeItem(at: savedRoot)
            try? fileManager.removeItem(at: knownRoot)
        }

        let suiteName = "AngelAppTests.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let resolver = AngelProjectRootResolver(
            defaults: defaults,
            fileManager: fileManager,
            knownRoots: [knownRoot]
        )

        try resolver.save(savedRoot)

        #expect(resolver.resolve() == savedRoot.resolvingSymlinksInPath().standardizedFileURL)
    }

    @Test
    func resolverRejectsFolderWithoutOrganizerScript() throws {
        let fileManager = FileManager.default
        let invalidRoot = fileManager.temporaryDirectory
            .appendingPathComponent("AngelAppTests-\(UUID().uuidString)", isDirectory: true)
        try fileManager.createDirectory(at: invalidRoot, withIntermediateDirectories: true)
        defer { try? fileManager.removeItem(at: invalidRoot) }

        let resolver = AngelProjectRootResolver(fileManager: fileManager, knownRoots: [])

        do {
            _ = try resolver.validate(invalidRoot)
            Issue.record("Expected a missing scripts/organize error.")
        } catch let error as AngelProjectRootError {
            #expect(error == .missingOrganizerScript(invalidRoot.standardizedFileURL))
        }
    }

    private func makeProjectRoot(fileManager: FileManager) throws -> URL {
        let root = fileManager.temporaryDirectory
            .appendingPathComponent("AngelAppTests-\(UUID().uuidString)", isDirectory: true)
        let scripts = root.appendingPathComponent("scripts", isDirectory: true)
        try fileManager.createDirectory(at: scripts, withIntermediateDirectories: true)
        let organizer = scripts.appendingPathComponent("organize")
        fileManager.createFile(atPath: organizer.path, contents: Data("#!/bin/sh\n".utf8))
        try fileManager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: organizer.path)
        return root
    }

}
