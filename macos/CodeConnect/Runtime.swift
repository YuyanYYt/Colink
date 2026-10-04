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

    var isBundled: Bool { python != nil }

    private struct Manifest: Decodable {
        let mode: String?
        let workspace: String?
        let uv: String?
        let python: String?
        let client: String
        let sampleRoot: String
        let chatURL: String
        let dataName: String?
    }

    static func load() throws -> RuntimeConfiguration {
        guard let file = Bundle.main.url(forResource: "runtime", withExtension: "json") else {
            throw NSError(domain: "Colink", code: 1)
        }
        let values = try JSONDecoder().decode(Manifest.self, from: Data(contentsOf: file))
        if values.mode == "bundled" {
            let manager = FileManager.default
            guard let resources = Bundle.main.resourceURL, let python = values.python else {
                throw NSError(domain: "Colink", code: 2)
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
                        python: resources.appendingPathComponent(python).path)
        }
        guard let workspace = values.workspace, let uv = values.uv else {
            throw NSError(domain: "Colink", code: 5)
        }
        return Self(workspace: workspace, uv: uv, client: values.client,
                    sampleRoot: values.sampleRoot, chatURL: values.chatURL, python: nil)
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
    let setupInput = ConnectionSetupInput()
    let configuration: RuntimeConfiguration
    var showPanel: (() -> Void)?
    var hidePanel: (() -> Void)?
    var didStop: (() -> Void)?
    private var connection: Process?
    private var controlPipe: Pipe?
    private var requestedStop = false
    private var timer: Timer?
    private let queue = DispatchQueue(label: "Colink.status", qos: .utility)

    var ownsConnection: Bool { connection?.isRunning == true }
    var isConfigured: Bool {
        FileManager.default.fileExists(atPath: configuration.workspace + "/.code-context/tunnel/profile.yaml")
        && FileManager.default.fileExists(atPath: configuration.workspace + "/.env.local")
    }
    var canStart: Bool {
        isConfigured && !ownsConnection && [.stopped, .failed].contains(phase)
    }
    var canChooseFolder: Bool {
        !ownsConnection && ![.preparing, .stopping, .external].contains(phase)
    }
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
        root = UserDefaults.standard.string(forKey: "SelectedFolder") ?? configuration.sampleRoot
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
        guard !checking else { return }
        guard isConfigured else { phase = .unconfigured; return }
        checking = true
        let selected = root
        let process = command(["desktop-status", "--workspace", configuration.workspace, "--root", selected])
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
                guard self.root == selected else { self.refresh(); return }
                guard let result else {
                    self.isReady = false
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
                if self.ownsConnection {
                    if self.phase != .stopping { self.phase = self.isReady ? .running : .starting }
                } else if result["external_active"] as? Bool == true || result["supervised"] as? Bool == true {
                    self.phase = .external
                } else if self.phase == .external {
                    self.phase = .stopped
                }
            }
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
            root = selected.standardizedFileURL.path
            UserDefaults.standard.set(root, forKey: "SelectedFolder")
            revision = 0
            fileCount = 0
            errorText = nil
            phase = .stopped
            refresh()
        }
        showPanel?()
    }

    func start() {
        guard canStart else { return }
        if URL(fileURLWithPath: root).standardizedFileURL.path != configuration.sampleRoot {
            let alert = NSAlert()
            alert.messageText = "启动这个文件夹的只读连接？"
            alert.informativeText = "\(root)\n\n该目录中通过过滤的源码会在网页查询时传给 OpenAI。只允许读取，不允许执行命令或修改文件。过滤不是完整的敏感信息检测；只保留当前和前一次代码状态。\n\n仅选择文件夹不会共享代码。"
            alert.alertStyle = .warning
            alert.addButton(withTitle: "确认并启动")
            alert.addButton(withTitle: "取消")
            NSApplication.shared.activate(ignoringOtherApps: true)
            guard alert.runModal() == .alertFirstButtonReturn else { return }
        }
        requestedStop = false
        errorText = nil
        phase = .preparing
        let process = command([
            "desktop-run", "--workspace", configuration.workspace, "--root", root,
            "--client", configuration.client, "--app-pid", String(ProcessInfo.processInfo.processIdentifier)
        ])
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
        // No persistent run flag, auto-start registration, or restart timer exists.
        guard ownsConnection else { return }
        phase = .stopping
        isReady = false
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
