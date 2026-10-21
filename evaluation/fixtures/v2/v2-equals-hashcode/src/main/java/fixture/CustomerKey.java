package fixture;

import java.util.Objects;

public class CustomerKey {
    private final int id;
    private final String name;

    public CustomerKey(int id, String name) {
        this.id = id;
        this.name = name;
    }

    @Override
    public boolean equals(Object other) {
        if (!(other instanceof CustomerKey)) return false;
        return id == ((CustomerKey) other).id;
    }

    @Override
    public int hashCode() {
        return Objects.hash(name);
    }
}
