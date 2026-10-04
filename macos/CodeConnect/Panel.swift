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
                        .accessibilityLabel("Colink SVG 标志")
                }
                VStack(alignment: .leading, spacing: 3) {
                    Text("Colink").font(.system(size: 20, weight: .semibold, design: .rounded))
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
            }
            .padding(15)
            .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 15))
            .overlay(RoundedRectangle(cornerRadius: 15).strokeBorder(.primary.opacity(0.07), lineWidth: 0.5))

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
