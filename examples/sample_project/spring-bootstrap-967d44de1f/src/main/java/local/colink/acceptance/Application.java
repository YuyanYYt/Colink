package local.colink.acceptance;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.CommandLineRunner;
import org.springframework.context.annotation.Bean;
import org.springframework.jdbc.core.JdbcTemplate;
@SpringBootApplication
public class Application {
  public static void main(String[] args) { SpringApplication.run(Application.class, args); }
  @Bean CommandLineRunner proveConnection(JdbcTemplate jdbc) {
    return args -> {
      String database = jdbc.queryForObject("SELECT current_database()", String.class);
      String user = jdbc.queryForObject("SELECT current_user", String.class);
      if (!database.equals(System.getenv("COLINK_DATABASE_NAME")) || !user.startsWith("colink_"))
        throw new IllegalStateException("Database scope did not match");
      jdbc.execute("CREATE TABLE products(id bigint PRIMARY KEY, title text NOT NULL)");
      jdbc.update("INSERT INTO products(id,title) VALUES (?,?)", 1L, "Spring Boot fixture");
      String title = jdbc.queryForObject("SELECT title FROM products WHERE id=1", String.class);
      if (!"Spring Boot fixture".equals(title)) throw new IllegalStateException("Roundtrip failed");
      System.out.println("COLINK_SPRING_DATABASE_ROUNDTRIP_OK " + database + " " + user);
    };
  }
}
