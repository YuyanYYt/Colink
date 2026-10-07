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
                    Toggle("允许运行项目", isOn: Binding(
                        get: { controller.executionEnabled },
                        set: { controller.setExecutionEnabled($0) }
                    ))
                    .toggleStyle(.switch).font(.system(size: 12, weight: .medium))
                    .disabled(!controller.canChangeExecution)
                    Text(controller.executionDetail).font(.system(size: 10)).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    Button("开发环境与数据库引导") { controller.showingDevelopmentSetup = true }
                        .font(.system(size: 11)).buttonStyle(.bordered)
                        .disabled(!controller.ownsConnection || !controller.isReady || controller.projectBusy)
                    ForEach(Array(controller.pendingPortReleases.enumerated()), id: \.offset) { _, plan in
                        VStack(alignment: .leading, spacing: 4) {
                            let numbers = (plan["ports"] as? [Int] ?? []).map(String.init).joined(separator: ", ")
                            Text("旧服务：\(plan["process"] as? String ?? "项目服务")（进程 \(plan["pid"] as? Int ?? 0)，端口 \(numbers)）")
                                .font(.system(size: 10)).textSelection(.enabled)
                            let force = plan["force_requested"] as? Bool == true
                            Text(force ? "先请求退出；仍未退出时允许强制终止。" : "仅请求正常退出；未确认停止会如实显示。")
                                .font(.system(size: 10)).foregroundStyle(.secondary)
                            Button(force ? "确认停止（包括强制）" : "确认停止") {
                                controller.confirmPortRelease(plan, allowForce: force)
                            }.font(.system(size: 10))
                        }
                    }
                    Divider()
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
                            .help(task)
                        HStack {
                            if controller.recoveryRequired {
                                Button("恢复未完成任务") { controller.recoverWrite() }
                                    .disabled(!controller.canRecoverWrite)
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
        .sheet(isPresented: $controller.showingExecutionAuthorization) {
            ExecutionAuthorizationView(controller: controller)
        }
        .sheet(isPresented: $controller.showingDevelopmentSetup) {
            DevelopmentSetupView(controller: controller)
        }
    }

}

private struct ExecutionAuthorizationView: View {
    @ObservedObject var controller: ConnectionController
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("允许哪些项目运行？").font(.system(size: 21, weight: .semibold))
            Text("允许依赖安装、测试、构建和开发服务。仅本次连接有效；关闭、切换或重启后撤销。")
                .font(.system(size: 12)).foregroundStyle(.secondary)
            ForEach(controller.writableProjects) { project in
                Toggle(project.selectionTitle, isOn: Binding(
                    get: { controller.selectedExecutionProjectIDs.contains(project.id) },
                    set: { if $0 { controller.selectedExecutionProjectIDs.insert(project.id) }
                           else { controller.selectedExecutionProjectIDs.remove(project.id) } }
                )).toggleStyle(.checkbox)
            }
            TextField("开发端口，用逗号分隔", text: $controller.executionPortsText)
            Text("2 个服务和 1 个有限任务；服务闲置 20 分钟停止。数据库连接另行配置。")
                .font(.system(size: 11)).foregroundStyle(.secondary)
            HStack {
                Button("取消") { controller.showingExecutionAuthorization = false }
                Spacer()
                Button("允许所选项目") { controller.confirmExecutionAuthorization() }
                    .buttonStyle(.borderedProminent).disabled(controller.selectedExecutionProjectIDs.isEmpty)
            }
        }.padding(24).frame(width: 430).tint(accent)
    }
}

private struct DevelopmentSetupView: View {
    @ObservedObject var controller: ConnectionController
    private var projectID: String {
        get { controller.setupProjectID }
        nonmutating set { controller.setupProjectID = newValue }
    }
    private var kind: String {
        get { controller.setupKind }
        nonmutating set { controller.setupKind = newValue }
    }
    private var port: String {
        get { controller.setupPort }
        nonmutating set { controller.setupPort = newValue }
    }
    private var database: String {
        get { controller.setupDatabase }
        nonmutating set { controller.setupDatabase = newValue }
    }
    private var user: String {
        get { controller.setupUser }
        nonmutating set { controller.setupUser = newValue }
    }
    private var password: String {
        get { controller.setupPassword }
        nonmutating set { controller.setupPassword = newValue }
    }
    private var tls: Bool {
        get { controller.setupTLS }
        nonmutating set { controller.setupTLS = newValue }
    }

    private var tools: [String: [String: Any]] {
        let environment = controller.developmentStatus["environment"] as? [String: Any] ?? [:]
        return environment["tools"] as? [String: [String: Any]] ?? [:]
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                Text("准备真实的项目环境").font(.system(size: 21, weight: .semibold))
                Picker("项目", selection: $controller.setupProjectID) {
                    ForEach(controller.writableProjects) { Text($0.selectionTitle).tag($0.id) }
                }.onChange(of: projectID) { _, value in controller.loadDevelopmentSetup(projectID: value) }
                Text("1. 查看实际工具，再选择运行方式").font(.headline)
                ForEach(tools.keys.sorted(), id: \.self) { name in
                    let tool = tools[name] ?? [:]
                    Text("\(name)：\(tool["available"] as? Bool == true ? (tool["version"] as? String ?? "已检测") : "未找到")")
                        .font(.system(size: 11)).textSelection(.enabled)
                }
                Text("Python 项目使用独立虚拟环境。同一任务的安装、测试和修改重跑复用环境，网页会先查询本机工具。")
                    .font(.system(size: 12)).foregroundStyle(.secondary)
                Text("2. 为项目准备数据库和专用账号").font(.headline)
                Text("优先复用已有 MySQL 或 PostgreSQL。向量项目推荐 PostgreSQL + pgvector。请在数据库中建立本项目的库和有限权限账号，再填下面的连接。数据库资料会保留。")
                    .font(.system(size: 12)).foregroundStyle(.secondary)
                Picker("数据库", selection: $controller.setupKind) {
                    Text("PostgreSQL + pgvector").tag("pgvector")
                    Text("PostgreSQL").tag("postgresql")
                    Text("MySQL").tag("mysql")
                    Text("Qdrant（连接配置，执行待支持）").tag("qdrant")
                }.onChange(of: kind) { _, value in port = value == "mysql" ? "43123" : value == "qdrant" ? "43124" : "43122" }
                Text("本机地址：127.0.0.1").font(.system(size: 12))
                TextField("端口", text: $controller.setupPort)
                Text("新建实例优先使用上方高位端口；连接已有数据库时，填写它实际监听的端口。")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
                TextField("项目库名（colink_ 开头）", text: $controller.setupDatabase)
                TextField("项目专用账号", text: $controller.setupUser)
                SecureField("密码或项目 API 密钥", text: $controller.setupPassword)
                Toggle("验证数据库 TLS", isOn: $controller.setupTLS)
                Text("密码只保存在本机钥匙串，网页只收到连接状态。请使用项目专用账号。")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
                Button("保存项目连接") {
                    if let number = Int(port) {
                        controller.configureDatabase(projectID: projectID, kind: kind, port: number,
                            database: database, user: user, password: password, tls: tls)
                        password = ""
                    }
                }.buttonStyle(.borderedProminent).disabled(projectID.isEmpty || password.isEmpty || controller.projectBusy)
                Text("3. 迁移、启动、验证读写").font(.headline)
                Text("连接保存后，让网页运行本项目的迁移，再启动后端。用接口实际写入和读取测试数据；重启后再次读取。向量项目还需确认扩展已安装并完成检索。工具存在或端口开放都不代表验证完成。")
                    .font(.system(size: 12)).foregroundStyle(.secondary)
                if let error = controller.projectError { Text(error).foregroundStyle(.orange).font(.system(size: 11)) }
                HStack { Spacer(); Button("完成") { password = ""; controller.showingDevelopmentSetup = false } }
            }.padding(24)
        }.frame(width: 490, height: 650).tint(accent)
        .onDisappear { password = "" }
        .onAppear { projectID = controller.writableProjects.first?.id ?? ""; if !projectID.isEmpty { controller.loadDevelopmentSetup(projectID: projectID) } }
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
                        Toggle(project.selectionTitle, isOn: Binding(
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
