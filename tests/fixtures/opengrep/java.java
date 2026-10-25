// Rule test for src/lemoncrow/pro/capabilities/review/security_rules/java.yaml.
// Run it with `opengrep scan --test --config <rules dir> tests/fixtures/opengrep`. Nothing here runs: it is sample
// text for the scanner.
import java.io.DataInputStream;
import java.io.InputStream;
import java.io.ObjectInputStream;
import java.security.MessageDigest;
import java.sql.Connection;
import java.sql.Statement;
import javax.crypto.Cipher;

class Sample {
    void command(String name) throws Exception {
        // ruleid: lemoncrow.java.runtime-exec-dynamic
        Runtime.getRuntime().exec("ls " + name);
        // ok: lemoncrow.java.runtime-exec-dynamic
        Runtime.getRuntime().exec("ls -l");
        // ok: lemoncrow.java.runtime-exec-dynamic
        Runtime.getRuntime().exec(new String[] {"ls", name});
    }

    void sql(Connection connection, String id, String name) throws Exception {
        Statement statement = connection.createStatement();
        // ruleid: lemoncrow.java.sql-string-building
        statement.executeQuery("SELECT * FROM users WHERE id = " + id);
        // ruleid: lemoncrow.java.sql-string-building
        statement.executeUpdate(String.format("DELETE FROM users WHERE name = '%s'", name));
        // ok: lemoncrow.java.sql-string-building
        connection.prepareStatement("SELECT * FROM users WHERE id = ?");
    }

    void read(InputStream in) throws Exception {
        // ruleid: lemoncrow.java.unsafe-deserialization
        Object value = new ObjectInputStream(in).readObject();
        System.out.println(value);
        // ok: lemoncrow.java.unsafe-deserialization
        int count = new DataInputStream(in).readInt();
        System.out.println(count);
    }

    void crypto() throws Exception {
        // ruleid: lemoncrow.java.weak-crypto
        MessageDigest.getInstance("MD5");
        // ruleid: lemoncrow.java.weak-crypto
        Cipher.getInstance("DES/CBC/PKCS5Padding");
        // ruleid: lemoncrow.java.weak-crypto
        Cipher.getInstance("AES");
        // ok: lemoncrow.java.weak-crypto
        MessageDigest.getInstance("SHA-256");
        // ok: lemoncrow.java.weak-crypto
        Cipher.getInstance("AES/GCM/NoPadding");
    }
}
