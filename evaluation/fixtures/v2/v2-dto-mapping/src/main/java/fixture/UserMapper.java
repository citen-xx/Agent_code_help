package fixture;

public class UserMapper {
    public UserDto toDto(User user) {
        return new UserDto(user.getId() + 1, user.getName(), null);
    }
}
