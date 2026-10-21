package fixture;

import java.util.HashMap;
import java.util.Map;
import java.util.Optional;

public class AddressRepository {
    private final Map<Integer, String> cities = new HashMap<>();
    public AddressRepository() { cities.put(1, "Shanghai"); }
    public Optional<String> findCity(int id) { return Optional.ofNullable(cities.get(id)); }
}
