package fixture;

import java.util.HashMap;
import java.util.Map;
import java.util.Optional;

public class CustomerRepository {
    private final Map<Integer, String> names = new HashMap<>();

    public CustomerRepository() {
        names.put(1, "Alice");
    }

    public Optional<String> findName(int id) {
        return Optional.ofNullable(names.get(id));
    }
}
