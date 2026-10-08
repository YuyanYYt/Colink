import AppKit
import Combine
import SwiftUI

private let accent = Color(nsColor: NSColor(name: "CodeConnectAccent") { appearance in
    if appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua {
        return NSColor(red: 0.37, green: 0.86, blue: 0.80, alpha: 1)
    }
    return NSColor(red: 0.04, green: 0.49, blue: 0.45, alpha: 1)
})

private struct PanelSheetBackground: NSViewRepresentable {
    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = .popover
        view.blendingMode = .behindWindow
        view.state = .active
        return view
    }
    func updateNSView(_ view: NSVisualEffectView, context: Context) {}
}

private struct PanelCardStyle: ViewModifier {
    func body(content: Content) -> some View {
        content.padding(15).frame(maxWidth: .infinity, alignment: .leading)
            .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 15))
            .overlay(RoundedRectangle(cornerRadius: 15)
                .strokeBorder(.primary.opacity(0.07), lineWidth: 0.5))
    }
}

private final class PanelButtonHover: ObservableObject {
    @Published var active = false
}

private struct PanelCardButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        PanelCardButtonBody(configuration: configuration)
    }
}

private struct PanelCardButtonBody: View {
    let configuration: ButtonStyleConfiguration
    @StateObject private var hover = PanelButtonHover()

    var body: some View {
        configuration.label
            .background(hover.active ? Color.primary.opacity(0.035) : Color.clear,
                        in: RoundedRectangle(cornerRadius: 15))
            .opacity(configuration.isPressed ? 0.7 : 1)
            .onHover { hover.active = $0 }
    }
}

private struct PanelDisclosureCard<Content: View>: View {
    let title: String
    let systemImage: String
    @Binding var expanded: Bool
    @ViewBuilder let content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                withAnimation(.easeInOut(duration: 0.16)) { expanded.toggle() }
            } label: {
                HStack(spacing: 10) {
                    Image(systemName: systemImage).foregroundStyle(.secondary)
                    Text(title).font(.system(size: 12, weight: .medium)).lineLimit(1)
                    Spacer(minLength: 0)
                    Image(systemName: "chevron.right").font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(.tertiary).rotationEffect(.degrees(expanded ? 90 : 0))
                }.padding(15).frame(maxWidth: .infinity, minHeight: 48)
                    .contentShape(Rectangle())
            }.buttonStyle(PanelCardButtonStyle())
                .accessibilityLabel(title).accessibilityValue(expanded ? "已展开" : "已折叠")
            if expanded { content().padding(.horizontal, 15).padding(.bottom, 15) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 15))
        .overlay(RoundedRectangle(cornerRadius: 15).strokeBorder(.primary.opacity(0.07), lineWidth: 0.5))
    }
}

private struct PanelModeButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        PanelModeButtonBody(configuration: configuration)
    }
}

private struct PanelModeButtonBody: View {
    let configuration: ButtonStyleConfiguration
    @Environment(\.isEnabled) private var enabled
    @StateObject private var hover = PanelButtonHover()

    var body: some View {
        configuration.label
            .background(hover.active && enabled ? Color.primary.opacity(0.04) : Color.clear,
                        in: RoundedRectangle(cornerRadius: 7))
            .overlay(RoundedRectangle(cornerRadius: 7)
                .strokeBorder(accent.opacity(hover.active && enabled ? 0.28 : 0), lineWidth: 0.75))
            .opacity(!enabled ? 0.55 : configuration.isPressed ? 0.72 : 1)
            .onHover { hover.active = $0 }
    }
}

private struct PanelModeControl: View {
    @ObservedObject var controller: ConnectionController

    var body: some View {
        HStack(spacing: 3) {
            ForEach(CodeAccessMode.allCases) { mode in
                Button { controller.requestMode(mode) } label: {
                    Text(mode.title).font(.system(size: 12, weight: .medium))
                        .frame(maxWidth: .infinity, minHeight: 32)
                        .padding(.horizontal, 6).contentShape(Rectangle())
                        .foregroundStyle(controller.accessMode == mode ? Color.white : Color.primary)
                        .background(controller.accessMode == mode ? accent : Color.clear,
                                    in: RoundedRectangle(cornerRadius: 7))
                }
                .buttonStyle(PanelModeButtonStyle())
                .disabled(!controller.canSelectMode(mode))
                .accessibilityValue(controller.accessMode == mode ? "已选中" : "未选中")
            }
        }
        .padding(3)
        .background(.quaternary.opacity(0.45), in: RoundedRectangle(cornerRadius: 9))
        .accessibilityElement(children: .contain).accessibilityLabel("代码访问模式")
    }
}

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

    private var panelWidth: CGFloat { 360 }
    private var panelHeight: CGFloat {
        guard controller.configuration.isLive else { return 440 }
        var height: CGFloat = 434
        if controller.taskDetail != nil { height += 28 }
        if controller.recoveryRequired { height += 30 }
        if controller.projectError != nil { height += 28 }
        if controller.phase == .failed || controller.phase == .external { height += 28 }
        height += CGFloat(controller.pendingPortReleases.count) * 48
        return height
    }

    var body: some View {
        mainPage
        .frame(width: panelWidth, height: panelHeight).tint(accent)
        .onAppear { controller.updatePanelSize?(panelWidth, panelHeight) }
        .onChange(of: panelHeight) { _, _ in controller.updatePanelSize?(panelWidth, panelHeight) }
        .sheet(isPresented: $controller.showingSetup) {
            ConnectionSetupView(controller: controller, input: controller.setupInput)
        }
        .sheet(isPresented: $controller.showingSettings) {
            StorageSettingsView(controller: controller)
        }
        .sheet(isPresented: $controller.showingProjects) {
            ProjectManagementView(controller: controller, input: controller.registrationInput)
        }
        .sheet(isPresented: $controller.showingModeAuthorization) {
            ModeAuthorizationView(controller: controller)
        }
    }

    private var mainPage: some View {
        VStack(alignment: .leading, spacing: 10) {
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
                Button {
                    if controller.isConfigured {
                        controller.storageExpanded = false
                        controller.showingSettings = true
                    } else {
                        controller.showingSetup = true
                    }
                } label: {
                    Image(systemName: "gearshape").frame(width: 25, height: 25).contentShape(Rectangle())
                }
                .buttonStyle(.plain).foregroundStyle(.secondary).accessibilityLabel("设置")
                Button { controller.hidePanel?() } label: {
                    Image(systemName: "xmark").font(.system(size: 10, weight: .semibold))
                        .frame(width: 25, height: 25).contentShape(Rectangle())
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
                        .lineSpacing(2).lineLimit(2).help(controller.detail)
                }
            }

            VStack(alignment: .leading, spacing: 8) {
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
                    .font(.system(size: 12, weight: .medium)).padding(.vertical, 6).padding(.horizontal, 10)
                    .frame(maxWidth: .infinity, minHeight: 32)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 8))
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
            .padding(14)
            .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 15))
            .overlay(RoundedRectangle(cornerRadius: 15).strokeBorder(.primary.opacity(0.07), lineWidth: 0.5))

            if controller.configuration.isLive {
                VStack(alignment: .leading, spacing: 10) {
                    PanelModeControl(controller: controller)
                    ForEach(Array(controller.pendingPortReleases.enumerated()), id: \.offset) { _, plan in
                        VStack(alignment: .leading, spacing: 4) {
                            let numbers = (plan["ports"] as? [Int] ?? []).map(String.init).joined(separator: ", ")
                            Text("\(plan["process"] as? String ?? "项目服务") · 端口 \(numbers)")
                                .font(.system(size: 10)).textSelection(.enabled)
                            let force = plan["force_requested"] as? Bool == true
                            Button(force ? "允许强制停止" : "停止旧服务") {
                                controller.confirmPortRelease(plan, allowForce: force)
                            }.font(.system(size: 10))
                        }
                    }
                    if let error = controller.projectError {
                        Text(error).font(.system(size: 10)).foregroundStyle(.orange).lineLimit(2).help(error)
                    }
                    if let task = controller.taskDetail {
                        Text(task).font(.system(size: 11)).lineLimit(2)
                            .foregroundStyle(.secondary)
                            .help(task)
                        if controller.recoveryRequired {
                            Button("恢复未完成任务") { controller.recoverWrite() }
                                .disabled(!controller.canRecoverWrite)
                                .font(.system(size: 11)).buttonStyle(.bordered)
                        }
                    }
                }
            }

            HStack(spacing: 10) {
                Button { controller.start() } label: {
                    Label("启动", systemImage: "play.fill")
                        .font(.system(size: 13, weight: .semibold)).frame(maxWidth: .infinity).frame(height: 38)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.borderedProminent).tint(accent).disabled(!controller.canStart)
                .accessibilityLabel("启动连接")
                Button { controller.stop() } label: {
                    Label("关闭", systemImage: "power")
                        .font(.system(size: 13, weight: .medium)).frame(maxWidth: .infinity).frame(height: 38)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.bordered)
                .disabled(!controller.ownsConnection || controller.phase == .stopping)
                .accessibilityLabel("关闭连接，停止所有连接和采集进程")
            }

            Spacer(minLength: 0)
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
        .padding(20).frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

private struct ModeAuthorizationView: View {
    @ObservedObject var controller: ConnectionController

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("开启\(controller.requestedMode.title)模式")
                .font(.system(size: 21, weight: .semibold))
            Text(controller.requestedMode == .development
                 ? "允许修改代码，并以当前 macOS 用户权限运行终端、安装依赖和操作数据库。终端可访问该用户有权限的文件与服务。"
                 : "允许修改代码。")
                .font(.system(size: 12)).foregroundStyle(.secondary)
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    ForEach(controller.writableProjects) { project in
                        Toggle(project.selectionTitle, isOn: Binding(
                            get: { controller.selectedModeProjectIDs.contains(project.id) },
                            set: { selected in
                                if selected { controller.selectedModeProjectIDs.insert(project.id) }
                                else { controller.selectedModeProjectIDs.remove(project.id) }
                            }
                        )).toggleStyle(.checkbox)
                    }
                }.frame(maxWidth: .infinity, alignment: .leading)
            }.frame(minHeight: 45, maxHeight: 160)
            Text("本次连接有效").font(.system(size: 11)).foregroundStyle(.secondary)
            HStack {
                Button("取消") { controller.showingModeAuthorization = false }
                Spacer()
                Button("开启") { controller.confirmModeAuthorization() }
                    .buttonStyle(.borderedProminent).disabled(!controller.canConfirmMode)
            }
        }.padding(24).frame(width: 410).tint(accent)
    }
}

private struct StorageSettingsView: View {
    @ObservedObject var controller: ConnectionController

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 12) {
                Image(systemName: "gearshape.fill")
                    .font(.system(size: 25)).symbolRenderingMode(.hierarchical).foregroundStyle(accent)
                Text("设置").font(.system(size: 21, weight: .semibold, design: .rounded))
                Spacer()
                Button { controller.showingSettings = false } label: {
                    Image(systemName: "xmark").font(.system(size: 10, weight: .semibold))
                        .frame(width: 25, height: 25).contentShape(Rectangle())
                }.buttonStyle(.plain).foregroundStyle(.tertiary).accessibilityLabel("关闭设置")
            }
            Divider().opacity(0.55)
            PanelDisclosureCard(title: "存储位置", systemImage: "folder", expanded: $controller.storageExpanded) {
                VStack(alignment: .leading, spacing: 12) {
                    Text(controller.configuration.workspace)
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                        .textSelection(.enabled).lineLimit(2).truncationMode(.middle)
                    HStack(spacing: 10) {
                        Button(controller.storageChanging ? "正在迁移…" : "更改位置") { controller.chooseStorage() }
                            .disabled(!controller.canChangeStorage)
                            .help("关闭连接后可更改存储位置")
                        Button("打开文件夹") { controller.openStorage() }
                    }.font(.system(size: 12)).buttonStyle(.bordered).controlSize(.large)
                    if let error = controller.storageError {
                        Text(error).font(.system(size: 11)).foregroundStyle(.orange)
                    }
                }.padding(.top, 12)
            }.font(.system(size: 12, weight: .medium))
        }.padding(20).frame(width: 400).tint(accent)
        .background(PanelSheetBackground()).presentationBackground(.clear)
        .onAppear { controller.storageExpanded = false }
        .onDisappear { controller.storageExpanded = false }
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
                                Text(project.selectionTitle).font(.system(size: 13, weight: .medium))
                                    .lineLimit(2)
                                Text(project.relativeRoot.isEmpty ? "." : project.relativeRoot)
                                    .font(.system(size: 10)).foregroundStyle(.secondary).lineLimit(1)
                                    .truncationMode(.middle).help(project.relativeRoot)
                                if project.status == "unavailable" {
                                    Text("暂不可用").font(.system(size: 10)).foregroundStyle(.orange)
                                }
                            }
                            Spacer(minLength: 8)
                            Text(project.enabled ? "允许" : "待确认")
                                .font(.system(size: 11))
                                .foregroundStyle(project.enabled ? accent : .secondary)
                            Toggle("允许网页读取 \(project.selectionTitle)", isOn: Binding(
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
