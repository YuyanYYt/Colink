import AppKit
import Foundation
import SwiftUI

struct RuntimeConfiguration {
    let workspace: String
    let uv: String
    let client: String
    let sampleRoot: String
    let chatURL: String
    let python: String?
    let sourceMode: String

    var isBundled: Bool { python != nil }
    var isLive: Bool { sourceMode == "live" }

    private struct Manifest: Decodable {
        let mode: String?
        let workspace: String?
        let uv: String?
        let python: String?
        let client: String
        let sampleRoot: String
        let chatURL: String
        let dataName: String?
        let sourceMode: String?
        let runtimeWorkspace: String?
    }

    static func load() throws -> RuntimeConfiguration {
        guard let file = Bundle.main.url(forResource: "runtime", withExtension: "json") else {
            throw NSError(domain: "Colink", code: 1)
        }
        let values = try JSONDecoder().decode(Manifest.self, from: Data(contentsOf: file))
        let sourceMode = values.sourceMode ?? "mirror"
        guard ["mirror", "live"].contains(sourceMode) else {
            throw NSError(domain: "Colink", code: 7)
        }
        if values.mode == "bundled" {
            let manager = FileManager.default
            guard let resources = Bundle.main.resourceURL, let python = values.python else {
                throw NSError(domain: "Colink", code: 2)
            }
            if let workspace = values.runtimeWorkspace {
                guard NSString(string: workspace).isAbsolutePath,
                      NSString(string: values.sampleRoot).isAbsolutePath else {
                    throw NSError(domain: "Colink", code: 8)
                }
                return Self(workspace: workspace, uv: "",
                            client: resources.appendingPathComponent(values.client).path,
                            sampleRoot: values.sampleRoot, chatURL: values.chatURL,
                            python: resources.appendingPathComponent(python).path,
                            sourceMode: sourceMode)
            }
            let support = try manager.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                          appropriateFor: nil, create: true)
            let name = values.dataName ?? "Colink"
            guard !name.contains("/"), name != ".", name != ".." else {
                throw NSError(domain: "Colink", code: 3)
            }
            let workspace = support.appendingPathComponent(name, isDirectory: true)
            if (try? workspace.resourceValues(forKeys: [.isSymbolicLinkKey]).isSymbolicLink) == true {
                throw NSError(domain: "Colink", code: 4)
            }
            let examples = workspace.appendingPathComponent("examples", isDirectory: true)
            let sample = examples.appendingPathComponent("sample_project", isDirectory: true)
            for directory in [examples, sample] {
                if (try? directory.resourceValues(forKeys: [.isSymbolicLinkKey]).isSymbolicLink) == true {
                    throw NSError(domain: "Colink", code: 6)
                }
            }
            try manager.createDirectory(at: sample, withIntermediateDirectories: true,
                                        attributes: [.posixPermissions: 0o700])
            for name in ["main.py", "models.py"] {
                let destination = sample.appendingPathComponent(name)
                if !manager.fileExists(atPath: destination.path) {
                    try manager.copyItem(at: resources.appendingPathComponent(values.sampleRoot)
                        .appendingPathComponent(name), to: destination)
                }
            }
            return Self(workspace: workspace.path, uv: "", client: resources.appendingPathComponent(values.client).path,
                        sampleRoot: sample.path, chatURL: values.chatURL,
                        python: resources.appendingPathComponent(python).path,
                        sourceMode: sourceMode)
        }
        guard let workspace = values.workspace, let uv = values.uv else {
            throw NSError(domain: "Colink", code: 5)
        }
        return Self(workspace: workspace, uv: uv, client: values.client,
                    sampleRoot: values.sampleRoot, chatURL: values.chatURL, python: nil,
                    sourceMode: sourceMode)
    }
}

struct WorkspaceProject: Identifiable {
    let id: String
    let displayName: String
    let relativeRoot: String
    let qualifiedName: String
    let enabled: Bool
    let status: String

    init?(_ value: [String: Any]) {
        guard let id = value["project_id"] as? String,
              let name = value["display_name"] as? String,
              let root = value["relative_root"] as? String,
              let enabled = value["enabled"] as? Bool else { return nil }
        self.id = id
        self.displayName = name
        self.relativeRoot = root
        self.qualifiedName = value["qualified_name"] as? String ?? root
        self.enabled = enabled
        self.status = value["status"] as? String ?? "unknown"
    }

    var selectionTitle: String {
        guard !relativeRoot.isEmpty, !qualifiedName.isEmpty,
              displayName != qualifiedName else { return displayName }
        return "\(displayName) · \(qualifiedName)"
    }
}

struct WorkspaceWriteTask: Equatable {
    let id: String
    let projectID: String
    let state: String

    init?(_ value: [String: Any]) {
        guard let id = value["task_id"] as? String, !id.isEmpty, id.count <= 256,
              let projectID = value["project_id"] as? String, !projectID.isEmpty,
              projectID.count <= 256, let state = value["state"] as? String else { return nil }
        self.id = id
        self.projectID = projectID
        self.state = state
    }

    var title: String {
        switch state {
        case "active": return "任务进行中"
        case "completed": return "最近任务已完成"
        case "rolled_back": return "任务已回退"
        case "rolling_back": return "任务回退待恢复"
        case "recovery_required": return "任务需要恢复"
        default: return "任务待确认"
        }
    }
}

enum ConnectionPhase: String {
    case unconfigured, stopped, preparing, starting, running, stopping, failed, external

    var title: String {
        switch self {
        case .unconfigured: return "先设置连接"
        case .stopped: return "已关闭"
        case .preparing, .starting: return "正在连接…"
        case .running: return "已连接"
        case .stopping: return "正在关闭…"
        case .failed: return "连接遇到问题"
        case .external: return "已有连接"
        }
    }
}

final class ConnectionSetupInput: ObservableObject {
    @Published var tunnelID = ""
    @Published var apiKey = ""
}

final class ProjectRegistrationInput: ObservableObject {
    @Published var relativeRoot = ""
    @Published var displayName = ""
}

// Cancellation state and process launch are protected by the same lock.
private final class LocalControlOperation: @unchecked Sendable {
    let process: Process
    private let lock = NSLock()
    private var cancelled = false

    init(_ process: Process) { self.process = process }

    func run() throws -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard !cancelled else { return false }
        try process.run()
        return true
    }

    func cancel() {
        lock.lock()
        defer { lock.unlock() }
        cancelled = true
        if process.isRunning { process.terminate() }
    }
}

@MainActor
final class ConnectionController: ObservableObject {
    @Published var root: String
    @Published var phase: ConnectionPhase = .stopped
    @Published var revision = 0
    @Published var fileCount = 0
    @Published var isReady = false
    @Published var errorText: String?
    @Published var checking = false
    @Published var configuring = false
    @Published var setupError: String?
    @Published var showingSetup = false
    @Published var showingProjects = false
    @Published var showingWriteAuthorization = false
    @Published var selectedWriteProjectIDs: Set<String> = []
    @Published var projects: [WorkspaceProject] = []
    @Published var writeEnabled = false
    @Published var writeAvailable = false
    @Published var recoveryRequired = false
    @Published var hasActiveTask = false
    @Published var activeTask: WorkspaceWriteTask?
    @Published var recentTask: WorkspaceWriteTask?
    @Published var localActions: Set<String> = []
    @Published var projectBusy = false
    @Published var projectError: String?
    let setupInput = ConnectionSetupInput()
    let registrationInput = ProjectRegistrationInput()
    let configuration: RuntimeConfiguration
    var showPanel: (() -> Void)?
    var hidePanel: (() -> Void)?
    var didStop: (() -> Void)?
    private var connection: Process?
    private var controlPipe: Pipe?
    private var requestedStop = false
    private var stateEpoch = 0
    private var localOperation: LocalControlOperation?
    private var acknowledgedWriteProjectIDs: Set<String> = []
    private var timer: Timer?
    private let queue = DispatchQueue(label: "Colink.status", qos: .utility)

    var ownsConnection: Bool { connection?.isRunning == true }
    var isConfigured: Bool {
        FileManager.default.fileExists(atPath: configuration.workspace + "/.code-context/tunnel/profile.yaml")
        && FileManager.default.fileExists(atPath: configuration.workspace + "/.env.local")
    }
    var canStart: Bool {
        isConfigured && !ownsConnection && !projectBusy && [.stopped, .failed].contains(phase)
    }
    var canChooseFolder: Bool {
        !ownsConnection && !projectBusy && ![.preparing, .stopping, .external].contains(phase)
    }
    var canManageProjects: Bool {
        configuration.isLive && ownsConnection && isReady && phase == .running
        && !projectBusy && !configuring && !hasActiveTask && !recoveryRequired && !writeEnabled
    }
    var writableProjects: [WorkspaceProject] {
        projects.filter { $0.enabled && $0.status != "unavailable" }
    }
    var canChangeWrite: Bool {
        configuration.isLive && ownsConnection && isReady && phase == .running
        && !projectBusy && (writeEnabled || (writeAvailable && !recoveryRequired
                                            && !writableProjects.isEmpty))
    }
    var canConfirmWrite: Bool {
        canChangeWrite && !writeEnabled && !selectedWriteProjectIDs.isEmpty
        && selectedWriteProjectIDs.isSubset(of: Set(writableProjects.map(\.id)))
    }
    var canRecoverWrite: Bool {
        configuration.isLive && ownsConnection && isReady && phase == .running
        && !projectBusy && recoveryRequired && activeTask != nil
        && localActions.contains("recover_write")
    }
    var taskDetail: String? {
        guard let task = activeTask ?? recentTask else { return nil }
        let name = projects.first(where: { $0.id == task.projectID })?.selectionTitle ?? task.projectID
        return "\(task.title) · \(name)"
    }
    private var modeArguments: [String] { configuration.isLive ? ["--mode", "live"] : [] }
    var detail: String {
        if let errorText { return errorText }
        switch phase {
        case .external: return "请先在原来的窗口中关闭连接。"
        case .failed: return "请检查文件夹与网络，再重试。"
        default: return ""
        }
    }

    init(configuration: RuntimeConfiguration) {
        self.configuration = configuration
        if configuration.isLive {
            root = configuration.sampleRoot
            let selection = URL(fileURLWithPath: configuration.workspace)
                .appendingPathComponent(".code-context/desktop/selection-live.json")
            if let metadata = try? selection.resourceValues(forKeys: [.fileSizeKey, .isSymbolicLinkKey]),
               metadata.isSymbolicLink != true, let size = metadata.fileSize, size <= 65536,
               let data = try? Data(contentsOf: selection), data.count <= 65536,
               let value = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let selected = value["selected_root"] as? String,
               selected.count <= 4096, NSString(string: selected).isAbsolutePath {
                root = selected
            }
        } else {
            root = UserDefaults.standard.string(forKey: "SelectedFolder") ?? configuration.sampleRoot
        }
        // Do not restore a running intent: every application launch is idle.
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
        refresh()
    }

    private func command(_ arguments: [String]) -> Process {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: configuration.python ?? configuration.uv)
        process.currentDirectoryURL = URL(fileURLWithPath: configuration.workspace)
        process.arguments = configuration.isBundled
            ? ["-I", "-B", "-m", "code_context"] + arguments
            : ["run", "--locked", "--no-sync", "colink"] + arguments
        // Existing credentials are never read by the UI. First-run secure input
        // is sent only over stdin; the launcher loads the private runtime file.
        var environment = ProcessInfo.processInfo.environment
        environment.removeValue(forKey: "OPENAI_API_KEY")
        environment.removeValue(forKey: "CONTROL_PLANE_API_KEY")
        environment.removeValue(forKey: "PYTHONPATH")
        environment.removeValue(forKey: "PYTHONHOME")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        process.environment = environment
        return process
    }

    func refresh() {
        guard !checking, !projectBusy else { return }
        guard isConfigured || configuration.isLive else { phase = .unconfigured; return }
        checking = true
        let selected = root
        let epoch = stateEpoch
        let process = command(["desktop-status", "--workspace", configuration.workspace, "--root", selected]
                              + modeArguments)
        let output = Pipe()
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        process.standardInput = FileHandle.nullDevice
        queue.async { [weak self] in
            var result: [String: Any]?
            do {
                try process.run()
                DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 12) {
                    if process.isRunning { process.terminate() }
                }
                let data = output.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                if process.terminationStatus == 0, data.count <= 65536 {
                    result = try JSONSerialization.jsonObject(with: data) as? [String: Any]
                }
            } catch { }
            DispatchQueue.main.async {
                guard let self else { return }
                self.checking = false
                guard self.root == selected, self.stateEpoch == epoch else { self.refresh(); return }
                guard let result else {
                    let hadWriteAuthorization = !self.acknowledgedWriteProjectIDs.isEmpty
                    self.isReady = false
                    self.clearWriteAuthorization()
                    self.writeAvailable = false
                    if self.ownsConnection && hadWriteAuthorization {
                        self.stop()
                        return
                    }
                    if self.ownsConnection && self.phase != .stopping {
                        self.phase = .failed
                        self.errorText = "文件夹暂时不可用。请关闭后重新选择。"
                    }
                    if !self.ownsConnection && self.phase != .preparing {
                        self.phase = .failed
                        self.errorText = "请确认应用所需的项目和所选文件夹仍在原位置。"
                    }
                    return
                }
                self.revision = result["revision"] as? Int ?? 0
                self.fileCount = result["tracked_files"] as? Int ?? 0
                self.isReady = result["ready"] as? Bool ?? false
                if !self.isReady && self.ownsConnection && !self.acknowledgedWriteProjectIDs.isEmpty {
                    self.stop()
                    return
                }
                self.applyWorkspaceStatus(result)
                if self.ownsConnection {
                    if self.phase != .stopping { self.phase = self.isReady ? .running : .starting }
                } else if result["external_active"] as? Bool == true || result["supervised"] as? Bool == true {
                    self.phase = .external
                } else if self.phase == .external {
                    self.phase = .stopped
                } else if !self.isConfigured {
                    self.phase = .unconfigured
                }
            }
        }
    }

    private func applyWorkspaceStatus(_ result: [String: Any]) {
        guard configuration.isLive else { return }
        let workspace = result["workspace_status"] as? [String: Any] ?? [:]
        projects = (workspace["projects"] as? [[String: Any]] ?? []).compactMap(WorkspaceProject.init)
        writeAvailable = workspace["write_available"] as? Bool ?? false
        recoveryRequired = workspace["recovery_required"] as? Bool ?? false
        hasActiveTask = workspace["active_task"] != nil && !(workspace["active_task"] is NSNull)
        activeTask = (workspace["active_task"] as? [String: Any]).flatMap(WorkspaceWriteTask.init)
        recentTask = (workspace["recent_task"] as? [String: Any]).flatMap(WorkspaceWriteTask.init)
        localActions = Set(workspace["local_actions"] as? [String] ?? [])
        selectedWriteProjectIDs.formIntersection(Set(writableProjects.map(\.id)))
        let grants = Set(workspace["write_projects"] as? [String] ?? [])
        let enabled = workspace["write_enabled"] as? Bool == true
        writeEnabled = enabled && writeAvailable && isReady && ownsConnection
            && !acknowledgedWriteProjectIDs.isEmpty && grants == acknowledgedWriteProjectIDs
            && grants.isSubset(of: Set(writableProjects.map(\.id)))
            && !requestedStop && phase != .stopping && !recoveryRequired
        if !enabled { acknowledgedWriteProjectIDs = [] }
        if enabled && !writeEnabled && ownsConnection {
            // Never show a disabled switch while an unacknowledged grant remains live.
            stop()
        }
    }

    private func runLocal(_ arguments: [String], payload: Data? = nil,
                          completion: @escaping ([String: Any]?) -> Void = { _ in }) {
        guard configuration.isLive, !projectBusy else { return }
        projectBusy = true
        projectError = nil
        stateEpoch += 1
        let epoch = stateEpoch
        let process = command(arguments + modeArguments)
        let input = payload == nil ? nil : Pipe()
        if let input { process.standardInput = input }
        else { process.standardInput = FileHandle.nullDevice }
        let output = Pipe()
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        let operation = LocalControlOperation(process)
        localOperation = operation
        queue.async { [weak self] in
            var result: [String: Any]?
            do {
                guard try operation.run() else { return }
                DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 15) {
                    if process.isRunning { process.terminate() }
                }
                if let input, let payload {
                    try input.fileHandleForWriting.write(contentsOf: payload)
                    try input.fileHandleForWriting.close()
                }
                let data = output.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                if process.terminationStatus == 0, data.count <= 65536 {
                    result = try JSONSerialization.jsonObject(with: data) as? [String: Any]
                }
            } catch { try? input?.fileHandleForWriting.close() }
            DispatchQueue.main.async {
                guard let self, self.localOperation === operation else { return }
                self.localOperation = nil
                self.projectBusy = false
                guard self.stateEpoch == epoch else { self.refresh(); return }
                if result == nil { self.projectError = "操作未完成，请刷新状态后重试。" }
                completion(result)
                self.refresh()
            }
        }
    }

    private func control(_ action: String, parameters: [String: Any] = [:],
                         completion: @escaping ([String: Any]?) -> Void = { _ in }) {
        guard let payload = try? JSONSerialization.data(withJSONObject: parameters) else { return }
        runLocal(["desktop-control", "--workspace", configuration.workspace, "--root", root,
                  "--action", action], payload: payload, completion: completion)
    }

    func discoverProjects() {
        guard canManageProjects else { return }
        control("discover")
    }

    func setProjectEnabled(_ project: WorkspaceProject, enabled: Bool) {
        guard canManageProjects else { return }
        control("set_enabled", parameters: ["project_id": project.id, "enabled": enabled])
    }

    func registerProject(relativeRoot: String, displayName: String) {
        guard canManageProjects else { return }
        control("register", parameters: ["relative_root": relativeRoot, "display_name": displayName])
    }

    func setWriteEnabled(_ enabled: Bool) {
        guard canChangeWrite, enabled != writeEnabled else { return }
        if enabled {
            selectedWriteProjectIDs = []
            showingWriteAuthorization = true
        } else {
            clearWriteAuthorization()
            control("disable_write") { [weak self] result in
                if result == nil { self?.stop() }
            }
        }
    }

    func confirmWriteAuthorization() {
        guard canConfirmWrite else { return }
        let projectIDs = selectedWriteProjectIDs.sorted()
        showingWriteAuthorization = false
        control("enable_write", parameters: ["project_ids": projectIDs]) { [weak self] result in
            guard let self else { return }
            guard result?["write_enabled"] as? Bool == true,
                  Set(result?["write_projects"] as? [String] ?? []) == Set(projectIDs) else {
                self.stop()
                return
            }
            self.acknowledgedWriteProjectIDs = Set(projectIDs)
            // A subsequent current-root desktop-status must also confirm these grants.
        }
    }

    private func clearWriteAuthorization() {
        writeEnabled = false
        acknowledgedWriteProjectIDs = []
        selectedWriteProjectIDs = []
        showingWriteAuthorization = false
    }

    func recoverWrite() {
        guard canRecoverWrite, let task = activeTask else { return }
        clearWriteAuthorization()
        control("disable_write") { [weak self] result in
            guard let self else { return }
            guard result != nil else { self.stop(); return }
            self.control("recover_write", parameters: ["project_id": task.projectID])
        }
    }


    func configure(tunnelID: String, apiKey: String, completion: @escaping () -> Void) {
        guard !configuring, !ownsConnection, !isConfigured else { return }
        guard let payload = try? JSONSerialization.data(withJSONObject: [
            "tunnel_id": tunnelID.trimmingCharacters(in: .whitespacesAndNewlines),
            "api_key": apiKey.trimmingCharacters(in: .whitespacesAndNewlines)
        ]) else { return }
        configuring = true
        setupError = nil
        let process = command(["desktop-setup", "--workspace", configuration.workspace])
        let input = Pipe()
        process.standardInput = input
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        queue.async { [weak self] in
            var success = false
            do {
                try process.run()
                DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 15) {
                    if process.isRunning { process.terminate() }
                }
                try input.fileHandleForWriting.write(contentsOf: payload)
                try input.fileHandleForWriting.close()
                process.waitUntilExit()
                success = process.terminationStatus == 0
            } catch { try? input.fileHandleForWriting.close() }
            DispatchQueue.main.async {
                guard let self else { return }
                self.configuring = false
                if success {
                    self.phase = .stopped
                    self.refresh()
                    completion()
                } else {
                    self.setupError = "未保存。请检查 ID、密钥及目录权限；已有配置不会被覆盖。"
                }
            }
        }
    }

    func chooseFolder() {
        guard canChooseFolder else { return }
        // Folder selection requires a stopped backend; its grants were revoked
        // before shutdown. Never carry a local authorization into another root.
        clearWriteAuthorization()
        hidePanel?()
        NSApplication.shared.activate(ignoringOtherApps: true)
        let chooser = NSOpenPanel()
        chooser.title = "选择要连接的代码文件夹"
        chooser.message = "选择文件夹后，点击启动即可连接。"
        chooser.prompt = "选择文件夹"
        chooser.canChooseDirectories = true
        chooser.canChooseFiles = false
        chooser.allowsMultipleSelection = false
        chooser.resolvesAliases = false
        chooser.directoryURL = URL(fileURLWithPath: root)
        if chooser.runModal() == .OK, let selected = chooser.url {
            let selectedRoot = selected.standardizedFileURL.path
            if configuration.isLive {
                runLocal(["desktop-select", "--workspace", configuration.workspace,
                          "--root", selectedRoot]) { [weak self] result in
                    if result != nil { self?.applySelection(selectedRoot) }
                }
            } else {
                UserDefaults.standard.set(selectedRoot, forKey: "SelectedFolder")
                applySelection(selectedRoot)
                refresh()
            }
        }
        showPanel?()
    }

    private func applySelection(_ selectedRoot: String) {
        stateEpoch += 1
        root = selectedRoot
        revision = 0
        fileCount = 0
        clearWriteAuthorization()
        writeAvailable = false
        activeTask = nil
        recentTask = nil
        hasActiveTask = false
        recoveryRequired = false
        localActions = []
        projects = []
        errorText = nil
        phase = isConfigured ? .stopped : .unconfigured
    }

    func start() {
        guard canStart else { return }
        if URL(fileURLWithPath: root).standardizedFileURL.path != configuration.sampleRoot {
            let alert = NSAlert()
            alert.messageText = configuration.isLive ? "启动这个工作区的连接？" : "启动这个文件夹的只读连接？"
            alert.informativeText = configuration.isLive
                ? "\(root)\n\n只有本机启用的项目可供网页按需读取。写入默认关闭；项目授权请在本机项目管理中设置。\n\n仅选择文件夹不会启动连接。"
                : "\(root)\n\n该目录中通过过滤的源码会在网页查询时传给 OpenAI。只允许读取，不允许执行命令或修改文件。过滤不是完整的敏感信息检测；只保留当前和前一次代码状态。\n\n仅选择文件夹不会共享代码。"
            alert.alertStyle = .warning
            alert.addButton(withTitle: "确认并启动")
            alert.addButton(withTitle: "取消")
            NSApplication.shared.activate(ignoringOtherApps: true)
            guard alert.runModal() == .alertFirstButtonReturn else { return }
        }
        requestedStop = false
        stateEpoch += 1
        clearWriteAuthorization()
        writeAvailable = false
        errorText = nil
        phase = .preparing
        let process = command([
            "desktop-run", "--workspace", configuration.workspace, "--root", root,
            "--client", configuration.client, "--app-pid", String(ProcessInfo.processInfo.processIdentifier)
        ] + modeArguments)
        let pipe = Pipe()
        process.standardInput = pipe
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        process.terminationHandler = { [weak self] finished in
            DispatchQueue.main.async {
                guard let self, self.connection === finished else { return }
                let stoppedIntentionally = self.requestedStop
                try? self.controlPipe?.fileHandleForWriting.close()
                self.controlPipe = nil
                self.connection = nil
                self.isReady = false
                self.clearWriteAuthorization()
                self.writeAvailable = false
                self.phase = finished.terminationStatus == 0 ? .stopped : .failed
                if self.phase == .failed {
                    self.errorText = stoppedIntentionally
                        ? "连接尚未完全关闭。请先检查后再启动。"
                        : "无法连接。请检查文件夹、网络及连接配置后重试。"
                }
                self.refresh()
                self.didStop?()
            }
        }
        connection = process
        controlPipe = pipe
        do {
            try process.run()
            phase = .starting
            refresh()
        } catch {
            connection = nil
            controlPipe = nil
            phase = .failed
            errorText = "无法启动本机运行环境。请确认项目目录没有移动。"
        }
    }

    func stop() {
        requestedStop = true
        stateEpoch += 1
        clearWriteAuthorization()
        writeAvailable = false
        localOperation?.cancel()
        localOperation = nil
        projectBusy = false
        // No persistent run flag, auto-start registration, or restart timer exists.
        guard ownsConnection else { return }
        phase = .stopping
        isReady = false
        if configuration.isLive {
            control("disable_write") { [weak self] _ in self?.finishOwnedStop() }
        } else {
            finishOwnedStop()
        }
    }

    private func finishOwnedStop() {
        // Even a failed local acknowledgement must close the owned connection.
        // Its supervisor independently revokes writes before stopping children.
        if let handle = controlPipe?.fileHandleForWriting {
            try? handle.write(contentsOf: Data("stop\n".utf8))
            try? handle.close()
        }
        controlPipe = nil
    }

    func openChat() {
        guard let url = URL(string: configuration.chatURL), url.host == "chatgpt.com" else { return }
        NSWorkspace.shared.open(url)
    }
}
