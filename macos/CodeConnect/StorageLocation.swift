import Foundation

enum StorageLocation {
    static let preferenceKey = "StorageLocation"
    static var defaultURL: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("CoLink-data", isDirectory: true)
    }
    static var savedURL: URL? {
        guard let path = UserDefaults.standard.string(forKey: preferenceKey),
              NSString(string: path).isAbsolutePath else { return nil }
        return URL(fileURLWithPath: path, isDirectory: true)
    }
    static var legacyURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Colink", isDirectory: true)
    }

    private static func error(_ message: String) -> NSError {
        NSError(domain: "CoLinkStorage", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
    }

    static func prepare(_ url: URL) throws {
        let manager = FileManager.default
        let path = url.standardizedFileURL
        guard path.path != "/", path != manager.homeDirectoryForCurrentUser,
              path.pathComponents.allSatisfy({ $0 != ".." }) else {
            throw error("请选择独立的 CoLink 存储文件夹。")
        }
        var current = URL(fileURLWithPath: "/", isDirectory: true)
        for component in path.pathComponents.dropFirst() {
            current.appendPathComponent(component, isDirectory: true)
            if let attributes = try? manager.attributesOfItem(atPath: current.path) {
                guard attributes[.type] as? FileAttributeType == .typeDirectory else {
                    throw error("存储位置必须是实际文件夹，不能使用链接。")
                }
            } else {
                try manager.createDirectory(at: current, withIntermediateDirectories: false,
                                            attributes: [.posixPermissions: 0o700])
            }
        }
        let attributes = try manager.attributesOfItem(atPath: path.path)
        guard (attributes[.ownerAccountID] as? NSNumber)?.uint32Value == getuid(),
              let permissions = attributes[.posixPermissions] as? NSNumber,
              permissions.intValue & 0o077 == 0 else {
            throw error("请选择仅当前用户可访问的存储文件夹。")
        }
    }

    static func relocate(_ configuration: RuntimeConfiguration, to destination: URL) throws {
        let target = destination.standardizedFileURL
        guard NSString(string: target.path).isAbsolutePath else {
            throw error("请选择有效的存储文件夹。")
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: configuration.python ?? configuration.uv)
        process.currentDirectoryURL = URL(fileURLWithPath: configuration.commandWorkspace)
        let arguments = [configuration.workspace, target.path]
        process.arguments = configuration.isBundled
            ? ["-I", "-B", "-m", "code_context.storage_location"] + arguments
            : ["run", "--locked", "--no-sync", "python", "-B", "-m", "code_context.storage_location"] + arguments
        var environment = ProcessInfo.processInfo.environment
        for name in ["OPENAI_API_KEY", "CONTROL_PLANE_API_KEY", "PYTHONPATH", "PYTHONHOME"] {
            environment.removeValue(forKey: name)
        }
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        process.environment = environment
        let output = Pipe()
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        process.standardInput = FileHandle.nullDevice
        try process.run()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard process.terminationStatus == 0, data.count <= 65536,
              let result = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              result["original_retained"] as? Bool == true,
              result["copied"] as? Bool != nil else {
            throw error("存储位置未更改。请先关闭连接、完成任务，并选择空文件夹；原数据已保留。")
        }
    }

    static func prepareSample(in workspace: URL, template: URL) throws {
        let manager = FileManager.default
        let sample = workspace.appendingPathComponent("examples/sample_project", isDirectory: true)
        try prepare(sample)
        for name in ["main.py", "models.py"] {
            let destination = sample.appendingPathComponent(name)
            if let attributes = try? manager.attributesOfItem(atPath: destination.path) {
                guard attributes[.type] as? FileAttributeType == .typeRegular else {
                    throw error("示例文件位置存在链接或其他文件；原资料已保留。")
                }
            } else {
                try manager.copyItem(at: template.appendingPathComponent(name), to: destination)
            }
        }
    }
}
