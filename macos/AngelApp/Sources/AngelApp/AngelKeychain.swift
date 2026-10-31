import Foundation
import Security

struct AppleReleaseCredentials: Sendable {
    let teamID: String
    let issuerID: String
    let keyID: String
    let privateKey: Data
}

enum AngelKeychainError: LocalizedError, Sendable {
    case invalidValue(String)
    case keychain(OSStatus)
    case missingCredentials

    var errorDescription: String? {
        switch self {
        case .invalidValue(let field):
            "Enter a value for \(field)."
        case .keychain(let status):
            "macOS Keychain returned status \(status)."
        case .missingCredentials:
            "No Apple release credentials are stored for Angel."
        }
    }
}

nonisolated enum AngelKeychain {
    private static let service = "com.maxj.angel.appstore-connect"
    private static let teamAccount = "team-id"
    private static let issuerAccount = "issuer-id"
    private static let keyAccount = "key-id"
    private static let privateKeyAccount = "private-key-p8"

    static func save(_ credentials: AppleReleaseCredentials) throws {
        guard !credentials.teamID.isEmpty else { throw AngelKeychainError.invalidValue("Team ID") }
        guard !credentials.issuerID.isEmpty else { throw AngelKeychainError.invalidValue("Issuer ID") }
        guard !credentials.keyID.isEmpty else { throw AngelKeychainError.invalidValue("Key ID") }
        guard !credentials.privateKey.isEmpty else { throw AngelKeychainError.invalidValue("private key") }

        try upsert(Data(credentials.teamID.utf8), account: teamAccount)
        try upsert(Data(credentials.issuerID.utf8), account: issuerAccount)
        try upsert(Data(credentials.keyID.utf8), account: keyAccount)
        try upsert(credentials.privateKey, account: privateKeyAccount)
    }

    static func hasCredentials() -> Bool {
        [teamAccount, issuerAccount, keyAccount, privateKeyAccount].allSatisfy { account in
            (try? read(account: account)) != nil
        }
    }

    static func load() throws -> AppleReleaseCredentials {
        guard let teamID = String(data: try read(account: teamAccount), encoding: .utf8), !teamID.isEmpty,
              let issuerID = String(data: try read(account: issuerAccount), encoding: .utf8), !issuerID.isEmpty,
              let keyID = String(data: try read(account: keyAccount), encoding: .utf8), !keyID.isEmpty else {
            throw AngelKeychainError.missingCredentials
        }

        return AppleReleaseCredentials(
            teamID: teamID,
            issuerID: issuerID,
            keyID: keyID,
            privateKey: try read(account: privateKeyAccount)
        )
    }

    static func delete() throws {
        for account in [teamAccount, issuerAccount, keyAccount, privateKeyAccount] {
            let status = SecItemDelete(query(account: account) as CFDictionary)
            guard status == errSecSuccess || status == errSecItemNotFound else {
                throw AngelKeychainError.keychain(status)
            }
        }
    }

    private static func upsert(_ data: Data, account: String) throws {
        let keychainQuery = query(account: account)
        let update: [String: Any] = [kSecValueData as String: data]
        let updateStatus = SecItemUpdate(keychainQuery as CFDictionary, update as CFDictionary)

        if updateStatus == errSecItemNotFound {
            var item = keychainQuery
            item[kSecValueData as String] = data
            item[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlocked
            let addStatus = SecItemAdd(item as CFDictionary, nil)
            guard addStatus == errSecSuccess else { throw AngelKeychainError.keychain(addStatus) }
        } else if updateStatus != errSecSuccess {
            throw AngelKeychainError.keychain(updateStatus)
        }
    }

    private static func read(account: String) throws -> Data {
        var item = query(account: account)
        item[kSecReturnData as String] = true
        item[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(item as CFDictionary, &result)
        guard status == errSecSuccess, let data = result as? Data else {
            throw AngelKeychainError.keychain(status)
        }
        return data
    }

    private static func query(account: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }
}
