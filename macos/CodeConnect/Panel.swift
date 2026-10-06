import AppKit
import SwiftUI

private let accent = Color(nsColor: NSColor(name: "CodeConnectAccent") { appearance in
    if appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua {
        return NSColor(red: 0.37, green: 0.86, blue: 0.80, alpha: 1)
    }
    return NSColor(red: 0.04, green: 0.49, blue: 0.45, alpha: 1)
})

struct ConnectPanel: View {
    @ObservedObject var controller: ConnectionController

    private var stateColor: Color {
        switch controller.phase {
        case .running: return accent
        case .failed: return .orange
        case .preparing, .starting, .stopping: return .blue
        default: return .secondary
        }
    }

    private var folderName: String { URL(fileURLWithPath: controller.root).lastPathComponent }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 12) {
                if let image = NSImage(named: "Logo") {
                    Image(nsImage: image).resizable().frame(width: 42, height: 42)
                        .accessibilityLabel("CoLink SVG 标志")
                }
                VStack(alignment: .leading, spacing: 3) {
                    Text("CoLink").font(.system(size: 20, weight: .semibold, design: .rounded))
                    Text("连接你的代码").font(.system(size: 11)).foregroundStyle(.secondary)
                }
                Spacer()
                if controller.configuration.isBundled && !controller.isConfigured {
                    Button { controller.showingSetup = true } label: {
                        Image(systemName: "gearshape").frame(width: 25, height: 25)
                    }
                    .buttonStyle(.plain).accessibilityLabel("首次连接设置")
                }
                Button { controller.hidePanel?() } label: {
                    Image(systemName: "xmark").font(.system(size: 10, weight: .semibold))
                        .frame(width: 25, height: 25)
                }
                .buttonStyle(.plain).foregroundStyle(.tertiary)
                .accessibilityLabel("收起面板，不停止连接")
            }

            Divider().opacity(0.55)

            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 9) {
                    Circle().fill(stateColor).frame(width: 7, height: 7)
                    Text(controller.phase.title)
                        .font(.system(size: 23, weight: .semibold, design: .rounded))
                        .contentTransition(.numericText())
                    Spacer()
                }
                if controller.phase == .failed || controller.phase == .external {
                    Text(controller.detail).font(.system(size: 11)).foregroundStyle(.secondary)
                        .lineSpacing(2).fixedSize(horizontal: false, vertical: true)
                }
            }

            VStack(alignment: .leading, spacing: 13) {
                HStack {
                    Text("文件夹").font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
                }
                HStack(alignment: .top, spacing: 11) {
                    Image(systemName: "folder.fill").font(.system(size: 26))
                        .symbolRenderingMode(.hierarchical).foregroundStyle(accent.opacity(0.85))
                    VStack(alignment: .leading, spacing: 5) {
                        Text(folderName).font(.system(size: 14, weight: .semibold)).lineLimit(1)
                        Text(controller.root).font(.system(size: 10))
                            .foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                            .textSelection(.enabled)
                            .help(controller.root)
                    }
                    Spacer(minLength: 0)
                }
                Button { controller.chooseFolder() } label: {
                    HStack {
                        Image(systemName: "folder.badge.plus")
                        Text("选择文件夹")
                        Spacer()
                        Image(systemName: "chevron.right").font(.system(size: 10, weight: .semibold))
                    }
                    .font(.system(size: 12, weight: .medium)).padding(.vertical, 8).padding(.horizontal, 10)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .background(.quaternary.opacity(0.45), in: RoundedRectangle(cornerRadius: 8))
                .disabled(!controller.canChooseFolder)
                .help("更换文件夹前，请先关闭连接")
                if controller.configuration.isLive {
                    Button { controller.showingProjects = true } label: {
                        Label("项目管理", systemImage: "square.stack.3d.up")
                            .font(.system(size: 12, weight: .medium))
                    }
                    .buttonStyle(.plain).foregroundStyle(accent)
                    .disabled(controller.projectBusy)
                }
            }
            .padding(15)
            .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 15))
            .overlay(RoundedRectangle(cornerRadius: 15).strokeBorder(.primary.opacity(0.07), lineWidth: 0.5))

            if controller.configuration.isLive {
                VStack(alignment: .leading, spacing: 6) {
                    Toggle("允许修改代码", isOn: Binding(
                        get: { controller.writeEnabled },
                        set: { controller.setWriteEnabled($0) }
                    ))
                    .toggleStyle(.switch).font(.system(size: 12, weight: .medium))
                    .disabled(!controller.canChangeWrite)
                    Text(controller.projectBusy ? "正在更新本机状态…"
                         : controller.recoveryRequired ? "需要先完成任务恢复。"
                         : !controller.writeAvailable ? "连接就绪后可开启。"
                         : controller.writeEnabled ? "仅允许修改你刚刚选择的项目。"
                         : "默认关闭，开启时选择允许修改的项目。")
                        .font(.system(size: 10)).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    if let error = controller.projectError {
                        Text(error).font(.system(size: 10)).foregroundStyle(.orange)
                    }
                    if let task = controller.taskDetail {
                        Text(task).font(.system(size: 11)).lineLimit(2)
                            .foregroundStyle(.secondary)
                        HStack {
                            if controller.recoveryRequired {
                                Button("恢复未完成任务") { controller.recoverWrite() }
                                    .disabled(!controller.canRecoverWrite)
                            } else {
                                Button("回退这项任务") { controller.rollbackWriteTask() }
                                    .disabled(!controller.canRollbackWrite)
                            }
                        }
                        .font(.system(size: 11)).buttonStyle(.bordered)
                    }
                }
            }

            HStack(spacing: 10) {
                Button { controller.start() } label: {
                    Label("启动", systemImage: "play.fill")
                        .font(.system(size: 13, weight: .semibold)).frame(maxWidth: .infinity).frame(height: 38)
                }
                .buttonStyle(.borderedProminent).tint(accent).disabled(!controller.canStart)
                .accessibilityLabel("启动连接")
                Button { controller.stop() } label: {
                    Label("关闭", systemImage: "power")
                        .font(.system(size: 13, weight: .medium)).frame(maxWidth: .infinity).frame(height: 38)
                }
                .buttonStyle(.bordered)
                .disabled(!controller.ownsConnection || controller.phase == .stopping)
                .accessibilityLabel("关闭连接，停止所有连接和采集进程")
            }

            Divider().opacity(0.55)

            HStack {
                Button { controller.openChat() } label: {
                    Label("打开 ChatGPT", systemImage: "arrow.up.right")
                        .font(.system(size: 10, weight: .medium))
                }
                .buttonStyle(.plain).foregroundStyle(.secondary)
                Spacer()
                Button("退出应用") { NSApplication.shared.terminate(nil) }
                    .font(.system(size: 10)).buttonStyle(.plain).foregroundStyle(.secondary)
            }
        }
        .padding(20)
        .frame(width: 360)
        .tint(accent)
        .sheet(isPresented: $controller.showingSetup) {
            ConnectionSetupView(controller: controller, input: controller.setupInput)
        }
        .sheet(isPresented: $controller.showingProjects) {
            ProjectManagementView(controller: controller, input: controller.registrationInput)
        }
        .sheet(isPresented: $controller.showingWriteAuthorization) {
            WriteAuthorizationView(controller: controller)
        }
    }

}

private struct WriteAuthorizationView: View {
    @ObservedObject var controller: ConnectionController

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("允许修改哪些项目？")
                .font(.system(size: 21, weight: .semibold, design: .rounded))
            Text("只授权本次连接。关闭或重启后需要重新开启。")
                .font(.system(size: 11)).foregroundStyle(.secondary)
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    ForEach(controller.writableProjects) { project in
                        Toggle(project.displayName, isOn: Binding(
                            get: { controller.selectedWriteProjectIDs.contains(project.id) },
                            set: { selected in
                                if selected { controller.selectedWriteProjectIDs.insert(project.id) }
                                else { controller.selectedWriteProjectIDs.remove(project.id) }
                            }
                        ))
                        .toggleStyle(.checkbox)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(minHeight: 50, maxHeight: 200)
            HStack {
                Button("取消") {
                    controller.selectedWriteProjectIDs = []
                    controller.showingWriteAuthorization = false
                }
                Spacer()
                Button("允许所选项目") { controller.confirmWriteAuthorization() }
                    .buttonStyle(.borderedProminent)
                    .disabled(!controller.canConfirmWrite)
            }
        }
        .padding(24).frame(width: 400).tint(accent)
    }
}

private struct ProjectManagementView: View {
    @ObservedObject var controller: ConnectionController
    @ObservedObject var input: ProjectRegistrationInput

    private var canRegister: Bool {
        let path = input.relativeRoot.trimmingCharacters(in: .whitespacesAndNewlines)
        let name = input.displayName.trimmingCharacters(in: .whitespacesAndNewlines)
        return controller.canManageProjects && !path.isEmpty && !name.isEmpty
            && !NSString(string: path).isAbsolutePath
            && !path.split(separator: "/").contains("..")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("项目管理").font(.system(size: 21, weight: .semibold, design: .rounded))
                Spacer()
                Button(controller.projectBusy ? "处理中…" : "发现候选") {
                    controller.discoverProjects()
                }
                .disabled(!controller.canManageProjects)
            }
            Text(controller.ownsConnection
                 ? "只有本机确认允许的项目可供网页读取，新发现的项目需要单独启用。"
                 : "启动连接后可发现和管理项目；选择文件夹不会自动共享代码。")
                .font(.system(size: 11)).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 10) {
                    if controller.projects.isEmpty {
                        Text("尚无项目，可发现候选或登记相对目录。")
                            .font(.system(size: 12)).foregroundStyle(.secondary)
                    }
                    ForEach(controller.projects) { project in
                        HStack(spacing: 12) {
                            VStack(alignment: .leading, spacing: 3) {
                                Text(project.displayName).font(.system(size: 13, weight: .medium))
                                    .lineLimit(1)
                                Text(project.relativeRoot.isEmpty ? "." : project.relativeRoot)
                                    .font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
                                if project.status == "unavailable" {
                                    Text("暂不可用").font(.system(size: 10)).foregroundStyle(.orange)
                                }
                            }
                            Spacer(minLength: 8)
                            Text(project.enabled ? "允许" : "待确认")
                                .font(.system(size: 11))
                                .foregroundStyle(project.enabled ? accent : .secondary)
                            Toggle("允许网页读取 \(project.displayName)", isOn: Binding(
                                get: { project.enabled },
                                set: { controller.setProjectEnabled(project, enabled: $0) }
                            ))
                            .labelsHidden().toggleStyle(.switch)
                            .disabled(!controller.canManageProjects)
                        }
                    }
                }
                .padding(.vertical, 4)
            }
            .frame(minHeight: 80, maxHeight: 240)
            Divider()
            TextField("项目相对目录，如 services/api", text: $input.relativeRoot)
                .textFieldStyle(.roundedBorder)
            TextField("可读名称", text: $input.displayName).textFieldStyle(.roundedBorder)
            if let error = controller.projectError {
                Text(error).font(.system(size: 11)).foregroundStyle(.orange)
            }
            HStack {
                Button("登记项目") {
                    controller.registerProject(
                        relativeRoot: input.relativeRoot.trimmingCharacters(in: .whitespacesAndNewlines),
                        displayName: input.displayName.trimmingCharacters(in: .whitespacesAndNewlines)
                    )
                }
                .disabled(!canRegister)
                Spacer()
                Button("完成") { controller.showingProjects = false }
            }
        }
        .padding(24).frame(width: 440).tint(accent)
        .onAppear { controller.refresh() }
    }
}

private struct ConnectionSetupView: View {
    @ObservedObject var controller: ConnectionController
    @ObservedObject var input: ConnectionSetupInput

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("连接设置").font(.system(size: 21, weight: .semibold, design: .rounded))
            Text("只需首次填写。保存后仍需手动点击启动。")
                .font(.system(size: 12)).foregroundStyle(.secondary)
            Link("打开官方私有隧道设置", destination: URL(string: "https://platform.openai.com/settings/organization/tunnels")!)
                .font(.system(size: 12))
            TextField("Tunnel ID", text: $input.tunnelID)
                .textFieldStyle(.roundedBorder).accessibilityLabel("私有隧道 ID")
            SecureField("运行 API Key", text: $input.apiKey)
                .textFieldStyle(.roundedBorder).accessibilityLabel("隧道运行密钥")
            Text("密钥仅存入本机私有文件，不上传到项目仓库。")
                .font(.system(size: 11)).foregroundStyle(.secondary)
            if let error = controller.setupError {
                Text(error).font(.system(size: 11)).foregroundStyle(.orange)
            }
            HStack {
                Button("取消") { input.apiKey = ""; controller.showingSetup = false }
                    .disabled(controller.configuring)
                Spacer()
                Button(controller.configuring ? "正在保存…" : "保存设置") {
                    controller.configure(tunnelID: input.tunnelID, apiKey: input.apiKey) {
                        input.apiKey = ""; controller.showingSetup = false
                    }
                    input.apiKey = ""
                }
                .buttonStyle(.borderedProminent).tint(accent)
                .disabled(controller.configuring || input.tunnelID.isEmpty || input.apiKey.isEmpty)
            }
        }
        .padding(24).frame(width: 380)
        .onDisappear { input.apiKey = "" }
    }
}
