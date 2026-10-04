import AppKit
import CoreServices
import SwiftUI

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSPopoverDelegate {
    private var statusItem: NSStatusItem!
    private var popover: NSPopover!
    private var controller: ConnectionController!
    private var quitting = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Register this concrete installed bundle, without resetting other apps' records.
        LSRegisterURL(Bundle.main.bundleURL as CFURL, true)
        let identifier = Bundle.main.bundleIdentifier ?? "local.codeconnect.menubar"
        if let previous = NSRunningApplication.runningApplications(withBundleIdentifier: identifier)
            .first(where: { $0.processIdentifier != ProcessInfo.processInfo.processIdentifier }) {
            previous.activate(options: [.activateAllWindows])
            NSApplication.shared.terminate(nil)
            return
        }
        do {
            let configuration = try RuntimeConfiguration.load()
            controller = ConnectionController(configuration: configuration)
        } catch {
            let alert = NSAlert()
            alert.messageText = "Colink 无法读取运行配置"
            alert.informativeText = "请从项目重新构建应用；不会启动任何连接。"
            alert.runModal()
            NSApplication.shared.terminate(nil)
            return
        }
        if let logoURL = Bundle.main.url(forResource: "logo", withExtension: "png"),
           let logo = NSImage(contentsOf: logoURL) { logo.setName("Logo") }

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let file = Bundle.main.url(forResource: "menubar", withExtension: "png"),
           let image = NSImage(contentsOf: file) {
            image.size = NSSize(width: 23, height: 23)
            image.isTemplate = true
            statusItem.button?.image = image
        } else {
            statusItem.button?.image = NSImage(systemSymbolName: "chevron.left.forwardslash.chevron.right", accessibilityDescription: "Colink")
        }
        statusItem.button?.toolTip = "Colink · 代码私有连接"
        statusItem.button?.setAccessibilityLabel("Colink 菜单栏")
        statusItem.button?.target = self
        statusItem.button?.action = #selector(togglePanel)
        statusItem.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])

        let hosting = NSHostingController(rootView: ConnectPanel(controller: controller))
        let material = NSVisualEffectView()
        material.material = .popover
        material.blendingMode = .behindWindow
        material.state = .active
        let container = NSViewController()
        container.view = material
        container.addChild(hosting)
        hosting.view.translatesAutoresizingMaskIntoConstraints = false
        material.addSubview(hosting.view)
        NSLayoutConstraint.activate([
            hosting.view.leadingAnchor.constraint(equalTo: material.leadingAnchor),
            hosting.view.trailingAnchor.constraint(equalTo: material.trailingAnchor),
            hosting.view.topAnchor.constraint(equalTo: material.topAnchor),
            hosting.view.bottomAnchor.constraint(equalTo: material.bottomAnchor)
        ])
        popover = NSPopover()
        popover.contentViewController = container
        popover.contentSize = NSSize(width: 360, height: 428)
        popover.behavior = .transient
        popover.delegate = self
        controller.showPanel = { [weak self] in self?.showPanel() }
        controller.hidePanel = { [weak self] in self?.popover.performClose(nil) }
        controller.didStop = { [weak self] in
            if self?.quitting == true { NSApplication.shared.reply(toApplicationShouldTerminate: true) }
        }
        installApplicationMenu()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in self?.showPanel() }
    }

    private func installApplicationMenu() {
        let mainMenu = NSMenu()
        let item = NSMenuItem()
        let applicationMenu = NSMenu(title: "Colink")
        let quit = NSMenuItem(title: "退出 Colink", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        applicationMenu.addItem(quit)
        item.submenu = applicationMenu
        mainMenu.addItem(item)
        NSApplication.shared.mainMenu = mainMenu
    }

    @objc private func togglePanel() {
        if NSApplication.shared.currentEvent?.type == .rightMouseUp {
            let menu = NSMenu()
            menu.addItem(withTitle: "打开 Colink", action: #selector(openPanel), keyEquivalent: "").target = self
            menu.addItem(.separator())
            menu.addItem(withTitle: "退出应用并关闭连接", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
            statusItem.menu = menu
            statusItem.button?.performClick(nil)
            statusItem.menu = nil
        } else if popover.isShown { popover.performClose(nil) }
        else { showPanel() }
    }

    @objc private func openPanel() { showPanel() }

    private func showPanel() {
        guard let button = statusItem?.button, popover != nil else { return }
        NSApplication.shared.activate(ignoringOtherApps: true)
        controller.refresh()
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        popover.contentViewController?.view.window?.makeKey()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showPanel()
        return true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard controller?.ownsConnection == true else { return .terminateNow }
        quitting = true
        controller.stop()
        return .terminateLater
    }
}

MainActor.assumeIsolated {
    let application = NSApplication.shared
    application.setActivationPolicy(.accessory)
    let delegate = AppDelegate()
    application.delegate = delegate
    application.run()
}
