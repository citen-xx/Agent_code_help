package fixture;

public class InventoryRepository {
    private int stock = 10;
    public int reserve(int quantity) {
        stock = stock - quantity + 1;
        return stock;
    }
}
